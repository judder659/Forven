<script lang="ts">
	import { onMount } from 'svelte';
	import { updateSettingsSection, type ForvenAgentModelOption } from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import { agentsConfig, connectedProviderIds } from '$lib/stores/agentsConfig';
	import { providerLabel } from '$lib/utils/agentsHub/agents';
	import { loadRouting, modelUsage, type RoutingAgent, type RoutingDraft } from '$lib/utils/agentsHub/routing';

	/** Tells the page about unsaved edits, so leaving asks first. */
	export let onDirtyChange: ((dirty: boolean) => void) | undefined = undefined;

	let pending: Set<string> | null = null;
	let search = '';
	let saving = false;
	let refreshing = false;
	let routing: { agents: RoutingAgent[]; draft: RoutingDraft } | null = null;
	let open: Record<string, boolean> = {};

	$: saved = $agentsConfig.enabledKeys;
	$: if (pending === null && !$agentsConfig.loading) pending = new Set(saved);
	$: current = pending ?? saved;
	$: added = [...current].filter((key) => !saved.has(key));
	$: removed = [...saved].filter((key) => !current.has(key));
	$: dirty = added.length + removed.length > 0;
	$: onDirtyChange?.(dirty);
	$: usage = routing ? modelUsage(routing.draft, routing.agents) : new Map<string, string[]>();
	$: words = search.toLowerCase().trim().split(/\s+/).filter(Boolean);
	$: matches = (option: ForvenAgentModelOption) => {
		const text = `${option.provider} ${providerLabel(option.provider)} ${option.label} ${option.model_id}`.toLowerCase();
		return words.every((word) => text.includes(word));
	};
	$: groups = [...$connectedProviderIds]
		.map((provider) => {
			const all = $agentsConfig.modelOptions.filter((option) => String(option.provider) === provider);
			return {
				provider,
				label: providerLabel(provider),
				all,
				shown: all.filter(matches).sort((a, b) => Number(current.has(b.key)) - Number(current.has(a.key)) || a.model_id.localeCompare(b.model_id)),
				listed: all.filter((option) => current.has(option.key)).length,
				inUse: all.filter((option) => usage.has(option.key)).length,
			};
		})
		.filter((group) => group.all.length > 0)
		.sort((a, b) => b.inUse - a.inUse || b.listed - a.listed || a.label.localeCompare(b.label));
	// Shortlisted models whose provider is not connected cannot be picked; offer to drop them.
	$: stranded = [...current].filter((key) => !$connectedProviderIds.has(key.slice(0, key.indexOf(':'))));

	onMount(async () => {
		await agentsConfig.ensureLoaded();
		const snapshot = await loadRouting($agentsConfig.policy);
		routing = { agents: snapshot.agents, draft: snapshot.draft };
	});

	function toggle(key: string) {
		const next = new Set(current);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		pending = next;
	}

	function setGroup(options: ForvenAgentModelOption[], on: boolean) {
		const next = new Set(current);
		for (const option of options) {
			if (on) next.add(option.key);
			else next.delete(option.key);
		}
		pending = next;
	}

	async function save() {
		if (!dirty) return;
		saving = true;
		try {
			await updateSettingsSection('agent-model-keys', { agent_model_keys: [...current] });
			agentsConfig.setEnabledKeys(new Set(current));
			addToast('Shortlist saved.', 'success');
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not save the shortlist.', 'error');
		} finally {
			saving = false;
		}
	}

	async function refresh() {
		refreshing = true;
		try {
			await agentsConfig.load({ refreshModels: true });
		} finally {
			refreshing = false;
		}
	}

	function isOpen(group: { provider: string; listed: number; inUse: number }, state: Record<string, boolean>, searching: boolean): boolean {
		if (searching) return true;
		return state[group.provider] ?? (group.listed > 0 || group.inUse > 0);
	}
</script>

<div class="grid gap-4" data-testid="setup-shortlist">
	<section class="flex flex-wrap items-end justify-between gap-3 rounded-md border border-sc-line bg-sc-panel px-3.5 py-3">
		<div class="min-w-0 max-w-[720px]">
			<h3 class="m-0 text-[13px] font-semibold text-sc-ink">Shortlist</h3>
			<p class="m-0 mt-0.5 text-[12px] text-sc-ink2">The models listed first in every picker, here and elsewhere in the app (the Bot Factory editor uses it too). It is optional: any model of a connected provider can still be picked.</p>
		</div>
		<div class="flex flex-wrap items-center gap-2">
			<input type="search" class="w-60 rounded-md border border-sc-line2 bg-sc-bg px-2.5 py-1 text-[12px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4" placeholder="Find a model or provider…" bind:value={search} aria-label="Find a model" />
			<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={refreshing} title="Ask every connected provider for its current model list" on:click={refresh}>{refreshing ? 'Refreshing…' : 'Refresh model lists'}</button>
		</div>
	</section>

	{#if stranded.length > 0}
		<p class="m-0 flex flex-wrap items-center justify-between gap-2 rounded-md border border-sc-line bg-sc-panel px-3.5 py-2 text-[12px] text-sc-ink3">
			<span>{stranded.length} shortlisted model{stranded.length === 1 ? '' : 's'} belong{stranded.length === 1 ? 's' : ''} to providers that are not connected, so no picker shows {stranded.length === 1 ? 'it' : 'them'}.</span>
			<button type="button" class="rounded border border-sc-line2 px-2 py-0.5 text-[11.5px] text-sc-ink2 hover:text-sc-ink" on:click={() => { const next = new Set(current); for (const key of stranded) next.delete(key); pending = next; }}>Remove them</button>
		</p>
	{/if}

	{#if $agentsConfig.loading && groups.length === 0}
		<div class="grid gap-2">{#each [0, 1, 2] as i (i)}<div class="h-10 animate-pulse rounded-md bg-sc-raise/60"></div>{/each}</div>
	{:else if groups.length === 0}
		<p class="m-0 rounded-md border border-sc-line bg-sc-panel px-3.5 py-5 text-center text-[12.5px] text-sc-ink2">Connect a provider first; its models show up here.</p>
	{:else}
		{#each groups as group (group.provider)}
			{@const expanded = isOpen(group, open, words.length > 0)}
			<section class="overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label={group.label} data-testid="setup-shortlist-group">
				<header class="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2">
					<button type="button" class="flex min-w-0 items-center gap-2 text-left" aria-expanded={expanded} on:click={() => (open = { ...open, [group.provider]: !expanded })}>
						<span class="w-3 text-[10px] text-sc-ink3" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
						<span class="text-[12.5px] font-semibold text-sc-ink">{group.label}</span>
						<span class="text-[11.5px] text-sc-ink3">{group.listed} of {group.all.length} shortlisted{group.inUse ? ` · ${group.inUse} in use` : ''}</span>
					</button>
					{#if expanded && group.shown.length > 0}
						<div class="flex gap-1.5 text-[11.5px]">
							<button type="button" class="text-sc-ink3 hover:text-sc-ink" on:click={() => setGroup(group.shown, true)}>Add all shown</button>
							<span class="text-sc-ink4">·</span>
							<button type="button" class="text-sc-ink3 hover:text-sc-ink" on:click={() => setGroup(group.shown, false)}>Clear shown</button>
						</div>
					{/if}
				</header>
				{#if expanded}
					{#if group.shown.length === 0}
						<p class="m-0 border-t border-sc-line px-3.5 py-2 text-[12px] text-sc-ink3">No model matches.</p>
					{:else}
						<ul class="m-0 grid list-none gap-px border-t border-sc-line bg-sc-line p-0 sm:grid-cols-2 xl:grid-cols-3">
							{#each group.shown as option (option.key)}
								{@const on = current.has(option.key)}
								{@const users = [...new Set((usage.get(option.key) ?? []).map((who) => who.replace(/ \(fallback\)$/, '')))]}
								<li class="bg-sc-panel">
									<label class="flex cursor-pointer items-center gap-2 px-3.5 py-1.5 hover:bg-sc-hover">
										<input type="checkbox" class="accent-[#7fb2ff]" checked={on} on:change={() => toggle(option.key)} aria-label={`Shortlist ${option.model_id}`} />
										<span class={`min-w-0 flex-1 truncate font-plex-mono text-[12px] ${on ? 'text-sc-ink' : 'text-sc-ink2'}`} title={option.label}>{option.model_id}</span>
										{#if users.length}
											<span class="shrink-0 rounded bg-[#3cc48f]/12 px-1.5 text-[10.5px] text-[#3cc48f]" title={`Used by ${users.join(', ')}`}>in use · {users.length === 1 ? users[0] : `${users.length} uses`}</span>
										{/if}
									</label>
								</li>
							{/each}
						</ul>
					{/if}
				{/if}
			</section>
		{/each}
	{/if}

	{#if dirty}
		<div class="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-2 rounded-md border border-sc-line2 bg-sc-panel2 px-3.5 py-2.5 shadow-2xl" data-testid="setup-shortlist-save">
			<span class="text-[12.5px] text-sc-ink">{added.length ? `${added.length} to add` : ''}{added.length && removed.length ? ' · ' : ''}{removed.length ? `${removed.length} to remove` : ''}</span>
			<div class="flex gap-2">
				<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink disabled:opacity-40" disabled={saving} on:click={() => (pending = new Set(saved))}>Discard</button>
				<button type="button" class="rounded-md bg-sc-ink px-3 py-1 text-[12px] font-medium text-black hover:bg-white disabled:opacity-40" disabled={saving} on:click={save}>{saving ? 'Saving…' : 'Save shortlist'}</button>
			</div>
		</div>
	{/if}
</div>
