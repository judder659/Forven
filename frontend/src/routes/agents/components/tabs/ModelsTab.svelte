<script lang="ts">
	/**
	 * Models tab. The enable-checkbox grid (agent_model_keys) grouped by
	 * provider, with a Refresh action. Enabling a model makes it selectable for
	 * agents and routing; disabling removes it from every picker. Reads/writes
	 * through the shared page-level store so the Roster / Routing pickers update
	 * the moment a checkbox flips.
	 */
	import { updateSettingsSection, type ForvenAgentModelOption } from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import { agentsConfig, connectedProviderIds } from '../agentsConfigStore';
	import DirtyBar from '../DirtyBar.svelte';

	// Notify the host page when this tab gains/loses unsaved edits, so it can
	// guard tab switches / navigation against silently discarding them (this tab
	// is destroyed on tab switch).
	export let onDirtyChange: ((dirty: boolean) => void) | undefined = undefined;

	$: modelOptions = $agentsConfig.modelOptions;
	$: storeKeys = $agentsConfig.enabledKeys;
	$: loading = $agentsConfig.loading;

	let saving = false;
	let error: string | null = null;
	let refreshing = false;

	// Batched edits: toggle as many models as you like, then save once.
	let pending: Set<string> | null = null;
	$: if (pending === null && (storeKeys.size > 0 || modelOptions.length > 0)) {
		pending = new Set(storeKeys);
	}
	function _setsEqual(a: Set<string>, b: Set<string>): boolean {
		return a.size === b.size && [...a].every((x) => b.has(x));
	}
	$: dirty = pending !== null && !_setsEqual(pending, storeKeys);
	// Surface the dirty state to the host so tab-switch / navigation can guard it.
	$: onDirtyChange?.(Boolean(dirty));

	function toggleModelKey(key: string, enabled: boolean) {
		const next = new Set(pending ?? storeKeys);
		if (enabled) next.add(key);
		else next.delete(key);
		pending = next;
	}

	async function save() {
		if (!pending || !dirty) return;
		saving = true;
		error = null;
		try {
			await updateSettingsSection('agent-model-keys', { agent_model_keys: [...pending] });
			agentsConfig.setEnabledKeys(new Set(pending)); // store update clears `dirty`
			addToast('Enabled models saved', 'success');
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to save model policy';
		} finally {
			saving = false;
		}
	}

	function discard() {
		pending = new Set(storeKeys);
	}

	async function refresh() {
		refreshing = true;
		try {
			await agentsConfig.load({ refreshModels: true });
			pending = new Set($agentsConfig.enabledKeys);
		} finally {
			refreshing = false;
		}
	}

	$: shownKeys = pending ?? storeKeys;
	$: grouped = modelOptions.reduce<Record<string, ForvenAgentModelOption[]>>((acc, opt) => {
		(acc[opt.provider] ??= []).push(opt);
		return acc;
	}, {});
</script>

<div class="space-y-6">
<section
	aria-labelledby="agents-models-heading"
	class="terminal-card p-6 space-y-4"
>
	<header class="border-b border-[#1a1a1a] pb-2 flex items-start justify-between gap-3">
		<div>
			<h2 id="agents-models-heading" class="text-sm font-bold uppercase tracking-widest text-white">Models</h2>
			<p class="text-xs text-[#666] mt-1">
				Enable the models that should be selectable for agents and routing.
				<span class="text-[#888]" title="Enabling a model makes it selectable everywhere on this page (agent dropdowns + routing pickers). Models from a provider you haven't connected can be enabled but still won't be usable until that provider is connected.">Enabling a model makes it selectable for agents/routing.</span>
			</p>
		</div>
		<button
			type="button"
			on:click={refresh}
			disabled={refreshing || loading}
			class="terminal-button text-xs px-2 py-1 disabled:opacity-60"
		>
			{refreshing ? 'Refreshing…' : 'Refresh from providers'}
		</button>
	</header>

	{#if error}
		<p class="text-xs text-red-400" role="alert">{error}</p>
	{/if}

	{#if loading && modelOptions.length === 0}
		<p class="text-sm text-[#666]">Loading available models…</p>
	{:else if modelOptions.length === 0}
		<p class="text-sm text-[#666]">
			No models discovered. Connect a provider under the Providers &amp; Keys tab.
		</p>
	{:else}
		<div class="space-y-4">
			{#each Object.entries(grouped) as [provider, opts] (provider)}
				{@const providerConnected = $connectedProviderIds.has(provider)}
				<div>
					<h3 class="text-xs font-semibold text-[#888] uppercase tracking-wider mb-2 flex items-center gap-2">
						{provider} <span class="text-[#555] font-normal">({opts.length})</span>
						{#if !providerConnected}
							<span class="text-[10px] normal-case tracking-normal text-yellow-400" title="Provider not connected — enabled models here stay unusable until you connect it under Providers & Keys.">not connected</span>
						{/if}
					</h3>
					<div class="grid gap-1 md:grid-cols-2 lg:grid-cols-3">
						{#each opts as opt (opt.key)}
							<label
								class="flex items-center gap-2 px-2 py-1.5 text-sm text-[#ccc] hover:bg-[#111] cursor-pointer"
							>
								<input
									type="checkbox"
									checked={shownKeys.has(opt.key)}
									disabled={saving}
									on:change={(e) => toggleModelKey(opt.key, (e.target as HTMLInputElement).checked)}
									class="accent-white"
								/>
								<span class="font-mono text-xs">{opt.label}</span>
							</label>
						{/each}
					</div>
				</div>
			{/each}
		</div>
	{/if}
</section>

	<DirtyBar
		dirty={Boolean(dirty)}
		{saving}
		message="You have unsaved model changes."
		saveLabel="Save enabled models"
		onSave={save}
		onDiscard={discard}
	/>
</div>
