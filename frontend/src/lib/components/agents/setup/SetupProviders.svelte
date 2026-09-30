<script lang="ts">
	import { onMount } from 'svelte';
	import {
		clearProviderHealth,
		deleteForvenAuthProvider,
		reconcileAgentProviders,
		testForvenAuthProvider,
		type AgentProviderWarning,
		type ForvenAuthProviderStatus,
		type ProviderRuntimeHealth,
	} from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import { agentsConfig, isProviderConnected } from '$lib/stores/agentsConfig';
	import { providerLabel } from '$lib/utils/agentsHub/agents';
	import { namesList, providerState } from '$lib/utils/agentsHub/providers';
	import { loadRouting, providerUsage, type RoutingAgent, type RoutingDraft } from '$lib/utils/agentsHub/routing';
	import { TONE_DOT, TONE_PILL } from '$lib/utils/forge/status';
	import ProviderConnect from './ProviderConnect.svelte';

	export let health: ProviderRuntimeHealth[] = [];
	export let warnings: AgentProviderWarning[] = [];
	/** Ask the page to re-read provider health after a change here. */
	export let onHealthChanged: (() => void) | undefined = undefined;

	let routing: { agents: RoutingAgent[]; draft: RoutingDraft } | null = null;
	let connecting: string | null = null;
	let reauthing: string | null = null;
	let busy: string | null = null;
	let testResult: Record<string, { ok: boolean; text: string }> = {};
	let now = Date.now();

	$: providers = $agentsConfig.providers;
	$: connected = providers.filter(isProviderConnected);
	$: available = providers.filter((provider) => !isProviderConnected(provider)).sort((a, b) => providerLabel(a.provider).localeCompare(providerLabel(b.provider)));
	$: runtimeOf = new Map(health.map((entry) => [String(entry.provider).toLowerCase(), entry]));
	$: usage = routing ? providerUsage(routing.draft, routing.agents) : new Map<string, string[]>();
	$: modelCount = countBy($agentsConfig.modelOptions.map((option) => String(option.provider)));
	$: shortlistCount = countBy([...$agentsConfig.enabledKeys].map((key) => key.slice(0, key.indexOf(':'))));
	$: ordered = [...connected].sort(
		(a, b) => (usage.get(String(b.provider))?.length ?? 0) - (usage.get(String(a.provider))?.length ?? 0) || providerLabel(a.provider).localeCompare(providerLabel(b.provider)),
	);

	function countBy(values: string[]): Map<string, number> {
		const counts = new Map<string, number>();
		for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
		return counts;
	}

	onMount(async () => {
		await agentsConfig.ensureLoaded();
		const snapshot = await loadRouting($agentsConfig.policy);
		routing = { agents: snapshot.agents, draft: snapshot.draft };
		now = Date.now();
	});

	async function afterConnect(provider: string) {
		connecting = null;
		reauthing = null;
		addToast(`${providerLabel(provider)} connected.`, 'success');
		await agentsConfig.load();
		onHealthChanged?.();
	}

	async function test(provider: string) {
		busy = `test:${provider}`;
		try {
			const res = await testForvenAuthProvider(provider);
			testResult = { ...testResult, [provider]: { ok: res.ok, text: res.message || (res.ok ? 'The key works.' : String(res.status)) } };
		} catch (error) {
			testResult = { ...testResult, [provider]: { ok: false, text: error instanceof Error ? error.message : 'The test failed.' } };
		} finally {
			busy = null;
		}
	}

	async function disconnect(provider: ForvenAuthProviderStatus) {
		const key = String(provider.provider);
		const users = usage.get(key) ?? [];
		const warning = users.length ? `\n\n${namesList(users, 6)} ${users.length === 1 ? 'uses' : 'use'} it and will fail until you move ${users.length === 1 ? 'it' : 'them'} to another provider.` : '';
		if (!confirm(`Disconnect ${providerLabel(key)}? Its saved credentials are removed and no agent can spend on it.${warning}`)) return;
		busy = `disconnect:${key}`;
		try {
			await deleteForvenAuthProvider(key);
			addToast(`${providerLabel(key)} disconnected.`, 'success');
			await agentsConfig.load();
			onHealthChanged?.();
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not disconnect.', 'error');
		} finally {
			busy = null;
		}
	}

	async function clearStatus(provider: string) {
		busy = `clear:${provider}`;
		try {
			await clearProviderHealth(provider);
			addToast(`Cleared ${providerLabel(provider)}'s status. The next call sets it again.`, 'success');
			onHealthChanged?.();
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not clear the status.', 'error');
		} finally {
			busy = null;
		}
	}

	async function reconcile() {
		busy = 'reconcile';
		try {
			const res = await reconcileAgentProviders();
			addToast(res.updated > 0 ? `Moved ${res.updated} agent${res.updated === 1 ? '' : 's'} to ${providerLabel(res.provider)} ${res.model_id ?? ''}.` : 'Nothing to move: every agent is on a connected provider.', 'success');
			await agentsConfig.load();
			onHealthChanged?.();
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not move the agents.', 'error');
		} finally {
			busy = null;
		}
	}

	function accessText(provider: ForvenAuthProviderStatus): string {
		if (provider.requires_token === false && !provider.supports_oauth) return 'Local server: needs its base URL';
		return provider.supports_oauth ? 'Sign in, or paste an API key' : 'Paste an API key';
	}
</script>

<div class="grid gap-4" data-testid="setup-providers">
	{#if warnings.length > 0}
		<section class="rounded-md border border-[#e7b24a]/40 bg-[#e7b24a]/[0.06] px-3.5 py-3" role="alert">
			<div class="flex flex-wrap items-start justify-between gap-3">
				<div class="min-w-0">
					<p class="m-0 text-[12.5px] font-medium text-[#e7b24a]">{warnings.length} agent{warnings.length === 1 ? ' is' : 's are'} set to a provider with no credentials</p>
					<ul class="m-0 mt-1 grid list-none gap-0.5 p-0 text-[12px] text-sc-ink2">
						{#each warnings as warning (warning.agent_id + warning.provider)}
							<li><span class="text-sc-ink">{warning.agent_id}</span> uses {providerLabel(warning.provider)}{warning.fallback ? `, falling back to ${warning.fallback}` : ', with no fallback: its runs fail'}.</li>
						{/each}
					</ul>
				</div>
				<button type="button" class="rounded-md border border-[#e7b24a]/50 px-3 py-1 text-[12px] text-[#e7b24a] hover:bg-[#e7b24a]/10 disabled:opacity-40" disabled={busy !== null} on:click={reconcile}>{busy === 'reconcile' ? 'Moving…' : 'Move them to a connected provider'}</button>
			</div>
		</section>
	{/if}

	<section aria-label="Connected providers">
		<div class="mb-2 flex flex-wrap items-baseline justify-between gap-2">
			<h3 class="m-0 text-[13px] font-semibold text-sc-ink">Connected <span class="font-normal text-sc-ink3">{connected.length}</span></h3>
			<span class="text-[11.5px] text-sc-ink3">Agents can only use models from these. Connecting a provider authorizes spend on it.</span>
		</div>
		{#if $agentsConfig.loading && providers.length === 0}
			<div class="grid gap-3 md:grid-cols-2">{#each [0, 1] as i (i)}<div class="h-28 animate-pulse rounded-md bg-sc-raise/60"></div>{/each}</div>
		{:else if connected.length === 0}
			<p class="m-0 rounded-md border border-sc-line bg-sc-panel px-3.5 py-5 text-center text-[12.5px] text-sc-ink2">No provider is connected yet. Connect one below and your agents can start running.</p>
		{:else}
			<div class="grid gap-3 md:grid-cols-2">
				{#each ordered as provider (provider.provider)}
					{@const key = String(provider.provider)}
					{@const state = providerState(provider, runtimeOf.get(key.toLowerCase()), now)}
					{@const users = usage.get(key) ?? []}
					<article class="grid content-start gap-2 rounded-md border border-sc-line bg-sc-panel px-3.5 py-3" data-testid="setup-provider-card">
						<header class="flex items-center justify-between gap-2">
							<h4 class="m-0 truncate text-[13px] font-semibold text-sc-ink">{providerLabel(key)}</h4>
							<span class={`inline-flex shrink-0 items-center gap-1.5 rounded border px-1.5 py-px text-[11px] ${TONE_PILL[state.tone]}`}><span class={`h-1.5 w-1.5 rounded-full ${TONE_DOT[state.tone]}`} aria-hidden="true"></span>{state.label}</span>
						</header>
						<p class="m-0 text-[11.5px] text-sc-ink3">{state.detail}</p>
						<dl class="m-0 grid grid-cols-[76px_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-[12px]">
							<dt class="text-sc-ink3">Used by</dt>
							<dd class="m-0 truncate text-sc-ink2" title={users.join(', ')}>{users.length ? namesList(users) : routing ? 'nothing yet' : '…'}</dd>
							<dt class="text-sc-ink3">Models</dt>
							<dd class="m-0 text-sc-ink2">{modelCount.get(key) ?? 0} available{#if shortlistCount.get(key)} · {shortlistCount.get(key)} on your shortlist{/if}</dd>
							{#if provider.expires_in}
								<dt class="text-sc-ink3">Sign-in</dt>
								<dd class="m-0 text-sc-ink2">{provider.expires_in}</dd>
							{/if}
							{#if provider.base_url}
								<dt class="text-sc-ink3">Server</dt>
								<dd class="m-0 truncate font-plex-mono text-[11.5px] text-sc-ink2">{provider.base_url}</dd>
							{/if}
						</dl>
						{#if testResult[key]}
							<p class={`m-0 text-[12px] ${testResult[key].ok ? 'text-[#3cc48f]' : 'text-[#f2956f]'}`}>{testResult[key].text}</p>
						{/if}
						{#if reauthing === key}
							<ProviderConnect {provider} startWithOAuth={Boolean(provider.supports_oauth)} on:connected={() => afterConnect(key)} on:cancel={() => (reauthing = null)} />
						{:else}
							<div class="flex flex-wrap gap-1.5 pt-0.5">
								<button type="button" class="rounded border border-sc-line2 px-2 py-0.5 text-[11.5px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={busy !== null} on:click={() => test(key)}>{busy === `test:${key}` ? 'Testing…' : 'Test'}</button>
								<button type="button" class={`rounded border px-2 py-0.5 text-[11.5px] disabled:opacity-40 ${state.key === 'reauth' ? 'border-[#7fb2ff]/50 text-[#a9cbff] hover:border-[#7fb2ff]' : 'border-sc-line2 text-sc-ink2 hover:border-sc-ink hover:text-sc-ink'}`} disabled={busy !== null} on:click={() => (reauthing = key)}>{provider.supports_oauth ? 'Sign in again' : 'Replace key'}</button>
								{#if state.key === 'down' || state.key === 'degraded'}
									<button type="button" class="rounded border border-sc-line2 px-2 py-0.5 text-[11.5px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={busy !== null} title="Forget the recorded failures; the next call records a fresh state." on:click={() => clearStatus(key)}>{busy === `clear:${key}` ? 'Clearing…' : 'Clear status'}</button>
								{/if}
								<span class="flex-1"></span>
								<button type="button" class="rounded border border-transparent px-2 py-0.5 text-[11.5px] text-sc-ink3 hover:border-[#e5574f]/50 hover:text-[#f2956f] disabled:opacity-40" disabled={busy !== null} on:click={() => disconnect(provider)}>{busy === `disconnect:${key}` ? 'Disconnecting…' : 'Disconnect'}</button>
							</div>
						{/if}
					</article>
				{/each}
			</div>
		{/if}
	</section>

	<section aria-label="Add a provider">
		<div class="mb-2 flex flex-wrap items-baseline justify-between gap-2">
			<h3 class="m-0 text-[13px] font-semibold text-sc-ink">Add a provider <span class="font-normal text-sc-ink3">{available.length}</span></h3>
			<span class="text-[11.5px] text-sc-ink3">Pick one, sign in or paste its key, and its models appear in every picker.</span>
		</div>
		<div class="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
			{#each available as provider (provider.provider)}
				{@const key = String(provider.provider)}
				<article class={`rounded-md border bg-sc-panel px-3.5 py-2.5 ${connecting === key ? 'border-sc-ink4 sm:col-span-2 xl:col-span-3' : 'border-sc-line'}`} data-testid="setup-provider-tile">
					<div class="flex items-center justify-between gap-3">
						<div class="min-w-0">
							<h4 class="m-0 truncate text-[12.5px] font-medium text-sc-ink">{providerLabel(key)}</h4>
							<p class="m-0 truncate text-[11px] text-sc-ink3">{accessText(provider)}{provider.configured ? ' · a key exists in the environment, but it is not connected here' : ''}</p>
						</div>
						{#if connecting !== key}
							<button type="button" class="shrink-0 rounded border border-sc-line2 px-2.5 py-0.5 text-[11.5px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink" on:click={() => (connecting = key)}>Connect</button>
						{/if}
					</div>
					{#if connecting === key}
						<div class="mt-2.5">
							<ProviderConnect {provider} on:connected={() => afterConnect(key)} on:cancel={() => (connecting = null)} />
						</div>
					{/if}
				</article>
			{/each}
		</div>
		{#if $agentsConfig.authFile}
			<p class="m-0 mt-2 text-[11px] text-sc-ink4">Credentials are stored in <span class="font-plex-mono">{$agentsConfig.authFile}</span>.</p>
		{/if}
	</section>
</div>
