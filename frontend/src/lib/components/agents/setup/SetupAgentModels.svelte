<script lang="ts">
	import { onMount } from 'svelte';
	import type { ProviderRuntimeHealth } from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import { agentJob, coverNote, providerLabel, type FallbackNote } from '$lib/utils/agentsHub/agents';
	import { outageCover, providerOf } from '$lib/utils/agentsHub/failover';
	import {
		AUX_KINDS,
		AUX_META,
		BRAIN_ID,
		cloneDraft,
		loadRouting,
		providerOfKey,
		routingChanges,
		saveRouting,
		type RoutingAgent,
		type RoutingDraft,
		type Slot,
	} from '$lib/utils/agentsHub/routing';
	import { TONE_DOT, TONE_TEXT, type Tone } from '$lib/utils/forge/status';
	import { agentsConfig, connectedProviderIds, pickableModelOptions } from '$lib/stores/agentsConfig';
	import FallbackList from './FallbackList.svelte';
	import ModelSelect from './ModelSelect.svelte';

	/** Runtime provider health, for the status column. */
	export let health: ProviderRuntimeHealth[] = [];
	/** Tells the page about unsaved edits, so leaving asks first. */
	export let onDirtyChange: ((dirty: boolean) => void) | undefined = undefined;
	/** Opens another Setup section. */
	export let onNavigate: ((section: 'providers' | 'models') => void) | undefined = undefined;

	let agents: RoutingAgent[] = [];
	let base: RoutingDraft | null = null;
	let draft: RoutingDraft | null = null;
	let loadErrors: string[] = [];
	let loading = true;
	let saving = false;
	let showChanges = false;
	let bulkKey = '';
	let bulkFallbacks: string[] = [];

	$: options = $pickableModelOptions;
	$: shortlist = $agentsConfig.enabledKeys;
	$: connected = $connectedProviderIds;
	$: labelOf = (key: string) => {
		const option = options.find((item) => item.key === key);
		return `${option?.model_id || key.slice(key.indexOf(':') + 1)} · ${providerLabel(providerOfKey(key))}`;
	};
	$: changes = base && draft ? routingChanges(base, draft, agents, labelOf) : [];
	$: onDirtyChange?.(changes.length > 0);
	$: policyMissing = !$agentsConfig.policy;

	async function load() {
		loading = true;
		await agentsConfig.ensureLoaded();
		const snapshot = await loadRouting($agentsConfig.policy);
		agents = snapshot.agents;
		base = snapshot.draft;
		draft = cloneDraft(snapshot.draft);
		loadErrors = snapshot.errors;
		loading = false;
	}

	onMount(() => {
		void load();
	});

	function setAgent(id: string, slot: Partial<Slot>) {
		if (!draft) return;
		draft = { ...draft, agents: { ...draft.agents, [id]: { ...draft.agents[id], ...slot } } };
	}

	function setAux(kind: (typeof AUX_KINDS)[number], slot: Partial<Slot>) {
		if (!draft) return;
		draft = { ...draft, aux: { ...draft.aux, [kind]: { ...draft.aux[kind], ...slot } } };
	}

	function setBackup(slot: Partial<Slot>) {
		if (!draft) return;
		draft = { ...draft, backup: { ...draft.backup, ...slot } };
	}

	/** Same rule as before the redesign: an empty chain here leaves each agent's own chain alone. */
	function applyToAll() {
		if (!draft || !bulkKey) return;
		const next = cloneDraft(draft);
		for (const agent of agents) {
			next.agents[agent.id].key = bulkKey;
			if (bulkFallbacks.length > 0) next.agents[agent.id].fallbacks = [...bulkFallbacks];
		}
		draft = next;
		addToast(`Every agent set to ${labelOf(bulkKey)}${bulkFallbacks.length ? ` with ${bulkFallbacks.length} fallback${bulkFallbacks.length === 1 ? '' : 's'}` : ''}. Review, then save.`, 'info');
	}

	async function save() {
		if (!base || !draft || changes.length === 0) return;
		saving = true;
		try {
			const updated = await saveRouting(base, draft, agents, $agentsConfig.policy);
			agentsConfig.setPolicy(updated);
			base = cloneDraft(draft);
			showChanges = false;
			void agentsConfig.load();
			addToast(`Saved ${changes.length} change${changes.length === 1 ? '' : 's'}.`, 'success');
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not save the model changes.', 'error', undefined, 9000);
		} finally {
			saving = false;
		}
	}

	function discard() {
		if (base) draft = cloneDraft(base);
		showChanges = false;
	}

	// ---- Status of a slot: is its model usable, and does it survive an outage?
	interface Status {
		tone: Tone;
		text: string;
		help: string;
	}

	$: healthOf = new Map(health.map((entry) => [String(entry.provider).toLowerCase(), entry]));

	function modelStatus(key: string, required: boolean, connectedIds: Set<string>, healthMap: Map<string, ProviderRuntimeHealth>): Status {
		if (!key) {
			return required
				? { tone: 'fail', text: 'No model', help: 'Pick a model, or this agent cannot run.' }
				: { tone: 'idle', text: '', help: '' };
		}
		const provider = providerOfKey(key);
		if (!connectedIds.has(provider)) {
			return { tone: 'fail', text: `${providerLabel(provider)} not connected`, help: 'Runs on this model fail until the provider is connected under Providers.' };
		}
		const state = healthMap.get(provider.toLowerCase());
		if (state?.state === 'down') return { tone: 'fail', text: `${providerLabel(provider)} down`, help: state.message || 'Recent calls failed.' };
		if (state?.state === 'degraded') return { tone: 'caution', text: `${providerLabel(provider)} degraded`, help: state.message || 'Recent calls hit limits or fell back.' };
		return { tone: 'ok', text: `${providerLabel(provider)} connected`, help: '' };
	}

	// Same rule as the roster: the agent's own fallbacks, then the backup.
	function cover(slot: Slot, backup: string, live: Set<string>): FallbackNote | null {
		return coverNote(outageCover(slot.key, slot.fallbacks, backup, live), slot.key);
	}

	$: live = new Set([...connected].map((id) => id.toLowerCase()));

	// ---- The three-step checklist at the top.
	$: required = agents.map((agent) => draft?.agents[agent.id]?.key ?? '');
	$: unassigned = required.filter((key) => !key || !connected.has(providerOfKey(key))).length;
	$: exposed = draft ? agents.filter((agent) => !cover(draft!.agents[agent.id], draft!.backup.key, live)?.covered).length : 0;
	$: providersInUse = draft ? [...new Set(Object.values(draft.agents).map((slot) => providerOfKey(slot.key)).filter(Boolean))] : [];
	$: backupCarries = draft ? agents.filter((agent) => {
		const slot = draft!.agents[agent.id];
		return outageCover(slot.key, slot.fallbacks, draft!.backup.key, live).via?.backup;
	}).length : 0;
	$: backupNote = describeBackup(draft?.backup.key ?? '', backupCarries, exposed, agents.length, live);

	/** What the backup does for this setup, in one line. */
	function describeBackup(key: string, carries: number, stranded: number, total: number, liveIds: Set<string>): { text: string; tone: Tone } {
		if (!key) return { text: 'Off: a run whose own model and fallbacks fail, fails.', tone: 'idle' };
		if (carries > 0) return { text: `The only way out for ${carries} of ${total} agents when their provider goes down.`, tone: 'ok' };
		if (stranded === 0) return { text: 'Every agent already moves to another provider on its own fallbacks.', tone: 'idle' };
		const provider = providerOf(key);
		if (!liveIds.has(provider)) return { text: `Covers no agent: ${providerLabel(provider)} is not connected.`, tone: 'caution' };
		return { text: `Covers no agent: the agents that need it run on ${providerLabel(provider)} too. Pick a model on another provider.`, tone: 'caution' };
	}
</script>

<div class="grid gap-4" data-testid="setup-agent-models">
	<section class="grid gap-px overflow-hidden rounded-md border border-sc-line bg-sc-line md:grid-cols-3" aria-label="Setup checklist">
		<div class="bg-sc-panel px-3.5 py-3">
			<p class="m-0 flex items-center gap-2 text-[12.5px] font-medium text-sc-ink">
				<span class={`grid h-4 w-4 place-items-center rounded-full text-[10px] ${connected.size > 0 ? 'bg-[#3cc48f]/20 text-[#3cc48f]' : 'bg-[#e7b24a]/20 text-[#e7b24a]'}`} aria-hidden="true">{connected.size > 0 ? '✓' : '1'}</span>
				Connect a provider
			</p>
			<p class="m-0 mt-1 text-[11.5px] text-sc-ink3">
				{connected.size > 0 ? `${connected.size} connected.` : 'None connected yet.'}
				<button type="button" class="text-sc-ink2 underline decoration-dotted underline-offset-2 hover:text-sc-ink" on:click={() => onNavigate?.('providers')}>{connected.size > 0 ? 'Manage providers' : 'Connect one'}</button>
			</p>
		</div>
		<div class="bg-sc-panel px-3.5 py-3">
			<p class="m-0 flex items-center gap-2 text-[12.5px] font-medium text-sc-ink">
				<span class={`grid h-4 w-4 place-items-center rounded-full text-[10px] ${!loading && unassigned === 0 ? 'bg-[#3cc48f]/20 text-[#3cc48f]' : 'bg-[#e7b24a]/20 text-[#e7b24a]'}`} aria-hidden="true">{!loading && unassigned === 0 ? '✓' : '2'}</span>
				Give every agent a model
			</p>
			<p class="m-0 mt-1 text-[11.5px] text-sc-ink3">
				{#if loading}Checking…{:else if unassigned === 0}All {agents.length} agents can run.{:else}{unassigned} agent{unassigned === 1 ? '' : 's'} cannot run: no model, or its provider is not connected.{/if}
			</p>
		</div>
		<div class="bg-sc-panel px-3.5 py-3">
			<p class="m-0 flex items-center gap-2 text-[12.5px] font-medium text-sc-ink">
				<span class={`grid h-4 w-4 place-items-center rounded-full text-[10px] ${!loading && exposed === 0 ? 'bg-[#3cc48f]/20 text-[#3cc48f]' : 'bg-[#e7b24a]/20 text-[#e7b24a]'}`} aria-hidden="true">{!loading && exposed === 0 ? '✓' : '3'}</span>
				Survive a provider outage
			</p>
			<p class="m-0 mt-1 text-[11.5px] text-sc-ink3">
				{#if loading}Checking…{:else if exposed === 0}Every agent can move to another provider.{:else if providersInUse.length === 1}{exposed} of {agents.length} agents stop if {providerLabel(providersInUse[0])} goes down. Add a fallback or a backup model on another provider.{:else}{exposed} of {agents.length} agents stop if their provider goes down. Add a fallback or a backup model on another provider.{/if}
			</p>
		</div>
	</section>

	{#if loadErrors.length > 0}
		<p class="m-0 rounded-md border border-[#e7b24a]/40 bg-[#e7b24a]/10 px-3 py-2 text-[12px] text-[#e7b24a]" role="alert">
			Could not load {loadErrors.join(', ')}.{#if policyMissing}{' '}Saving is off until it loads, so stored fallbacks are never overwritten.{/if}
			<button type="button" class="ml-1 underline" on:click={() => void load()}>Retry</button>
		</p>
	{/if}

	{#if loading || !draft}
		<div class="grid gap-2" aria-busy="true">{#each [0, 1, 2, 3, 4] as i (i)}<div class="h-12 animate-pulse rounded-md bg-sc-raise/60"></div>{/each}</div>
	{:else}
		<section class="overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label="Agents">
			<header class="flex flex-wrap items-baseline justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
				<div>
					<h3 class="m-0 text-[13px] font-semibold text-sc-ink">Agents</h3>
					<p class="m-0 text-[11.5px] text-sc-ink3">Each agent's model, and where its runs go when that model fails. Fallbacks take over only if the model fails before the run has used a tool; a failure after that ends the run.</p>
				</div>
				<span class="text-[11px] text-sc-ink3">Pickers list every model of a connected provider; your shortlist comes first.</span>
			</header>
			<div class="hidden grid-cols-[minmax(0,1.1fr)_minmax(0,1.3fr)_minmax(0,1.5fr)_minmax(0,0.9fr)] gap-3 border-b border-sc-line px-3.5 py-1.5 text-[11px] text-sc-ink3 lg:grid">
				<span>Agent</span><span>Model</span><span>Fallbacks, in order</span><span>Status</span>
			</div>
			<ul class="m-0 list-none divide-y divide-sc-line p-0">
				{#each agents as agent (agent.id)}
					{@const slot = draft.agents[agent.id]}
					{@const status = modelStatus(slot.key, true, connected, healthOf)}
					{@const note = cover(slot, draft.backup.key, live)}
					{@const changed = base ? slot.key !== base.agents[agent.id]?.key || slot.fallbacks.join('|') !== base.agents[agent.id]?.fallbacks.join('|') : false}
					<li class={`grid items-start gap-3 px-3.5 py-2.5 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1.3fr)_minmax(0,1.5fr)_minmax(0,0.9fr)] ${changed ? 'bg-[#7fb2ff]/[0.04]' : ''}`} data-testid="setup-agent-row">
						<div class="min-w-0">
							<div class="flex items-center gap-2">
								<span class="truncate text-[12.5px] font-medium text-sc-ink">{agent.name}</span>
								{#if changed}<span class="rounded bg-[#7fb2ff]/15 px-1 text-[10px] text-[#a9cbff]">edited</span>{/if}
							</div>
							<p class="m-0 text-[11px] text-sc-ink3">{agent.id === BRAIN_ID ? 'Orchestrator · also the default model' : agentJob(agent.id).job}</p>
						</div>
						<ModelSelect value={slot.key} {options} {shortlist} unsetLabel={slot.key ? null : 'Choose a model'} ariaLabel={`${agent.name} model`} on:change={(event) => setAgent(agent.id, { key: event.detail.value })} />
						<div class="min-w-0">
							<FallbackList fallbacks={slot.fallbacks} primary={slot.key} {options} {shortlist} ariaLabel={`${agent.name} fallback`} on:change={(event) => setAgent(agent.id, { fallbacks: event.detail.fallbacks })} />
						</div>
						<div class="min-w-0 text-[11.5px]">
							<p class={`m-0 flex items-center gap-1.5 ${TONE_TEXT[status.tone]}`} title={status.help}><span class={`h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[status.tone]}`} aria-hidden="true"></span><span class="truncate">{status.text}</span></p>
							{#if note}
								<p class={`m-0 mt-0.5 ${TONE_TEXT[note.covered ? 'ok' : note.tone]}`} title={note.help}>{note.text}</p>
							{/if}
						</div>
					</li>
				{/each}
			</ul>
			<details class="border-t border-sc-line bg-sc-panel2/40">
				<summary class="cursor-pointer px-3.5 py-2 text-[12px] text-sc-ink2 hover:text-sc-ink">Use one model for every agent…</summary>
				<div class="grid items-start gap-3 px-3.5 pb-3 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1.3fr)_minmax(0,1.5fr)_minmax(0,0.9fr)]">
					<p class="m-0 text-[11.5px] text-sc-ink3">Sets every agent above, the Brain included. An empty fallback list leaves each agent's own fallbacks as they are.</p>
					<ModelSelect value={bulkKey} {options} {shortlist} unsetLabel="Choose a model" ariaLabel="Model for every agent" on:change={(event) => (bulkKey = event.detail.value)} />
					<FallbackList fallbacks={bulkFallbacks} primary={bulkKey} {options} {shortlist} ariaLabel="Fallback for every agent" on:change={(event) => (bulkFallbacks = event.detail.fallbacks)} />
					<div>
						<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={!bulkKey} on:click={applyToAll}>Apply to all {agents.length}</button>
					</div>
				</div>
			</details>
		</section>

		<section class="overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label="Background tasks">
			<header class="border-b border-sc-line px-3.5 py-2.5">
				<h3 class="m-0 text-[13px] font-semibold text-sc-ink">Background tasks</h3>
				<p class="m-0 text-[11.5px] text-sc-ink3">Small jobs the Brain hands off. Left on the built-in default, they use OpenRouter models; if OpenRouter is not connected the call moves to a connected model, or fails.</p>
			</header>
			<ul class="m-0 list-none divide-y divide-sc-line p-0">
				{#each AUX_KINDS as kind (kind)}
					{@const slot = draft.aux[kind]}
					{@const status = modelStatus(slot.key, false, connected, healthOf)}
					<li class="grid items-start gap-3 px-3.5 py-2.5 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1.3fr)_minmax(0,1.5fr)_minmax(0,0.9fr)]" data-testid="setup-aux-row">
						<div class="min-w-0">
							<p class="m-0 truncate text-[12.5px] font-medium text-sc-ink">{AUX_META[kind].label}</p>
							<p class="m-0 text-[11px] text-sc-ink3">{AUX_META[kind].help}</p>
						</div>
						<ModelSelect value={slot.key} {options} {shortlist} unsetLabel="Built-in default" ariaLabel={`${AUX_META[kind].label} model`} on:change={(event) => setAux(kind, { key: event.detail.value })} />
						<FallbackList fallbacks={slot.fallbacks} primary={slot.key} {options} {shortlist} ariaLabel={`${AUX_META[kind].label} fallback`} on:change={(event) => setAux(kind, { fallbacks: event.detail.fallbacks })} />
						<p class={`m-0 flex items-center gap-1.5 text-[11.5px] ${TONE_TEXT[status.tone]}`} title={status.help}>
							{#if status.text}<span class={`h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[status.tone]}`} aria-hidden="true"></span>{status.text}{:else}<span class="text-sc-ink3">built-in default</span>{/if}
						</p>
					</li>
				{/each}
			</ul>
		</section>

		<section class="overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label="Safety net">
			<header class="border-b border-sc-line px-3.5 py-2.5">
				<h3 class="m-0 text-[13px] font-semibold text-sc-ink">Safety net</h3>
				<p class="m-0 text-[11.5px] text-sc-ink3">One model, tried last when a run's own model and fallbacks fail, for every agent including the Brain. A run whose model or fallbacks already use its provider skips it, so it helps only on a provider your agents do not use. Off: those runs fail and show up in Needs you.</p>
			</header>
			{#if draft.backup}
				{@const status = modelStatus(draft.backup.key, false, connected, healthOf)}
				<div class="grid items-start gap-3 px-3.5 py-2.5 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1.3fr)_minmax(0,1.5fr)_minmax(0,0.9fr)]" data-testid="setup-backup-row">
					<p class="m-0 text-[12.5px] font-medium text-sc-ink">Backup model</p>
					<ModelSelect value={draft.backup.key} {options} {shortlist} unsetLabel="Off" ariaLabel="Backup model" on:change={(event) => setBackup({ key: event.detail.value })} />
					<p class={`m-0 text-[11.5px] ${backupNote.tone === 'idle' ? 'text-sc-ink3' : TONE_TEXT[backupNote.tone]}`} data-testid="setup-backup-note">{backupNote.text}</p>
					<p class={`m-0 flex items-center gap-1.5 text-[11.5px] ${TONE_TEXT[status.tone]}`} title={status.help}>
						{#if status.text}<span class={`h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[status.tone]}`} aria-hidden="true"></span>{status.text}{:else}<span class="text-sc-ink3">off</span>{/if}
					</p>
				</div>
			{/if}
		</section>
	{/if}

	{#if changes.length > 0}
		<div class="sticky bottom-0 z-10 rounded-md border border-sc-line2 bg-sc-panel2 shadow-2xl" data-testid="setup-review">
			{#if showChanges}
				<ul class="m-0 max-h-56 list-none divide-y divide-sc-line overflow-y-auto border-b border-sc-line p-0 text-[12px]">
					{#each changes as change (change.slot + change.what)}
						<li class="grid grid-cols-[minmax(0,180px)_minmax(0,1fr)] gap-3 px-3.5 py-1.5">
							<span class="truncate text-sc-ink2">{change.who} · {change.what === 'model' ? 'model' : 'fallbacks'}</span>
							<span class="min-w-0 truncate font-plex-mono text-[11.5px]"><span class="text-sc-ink3 line-through">{change.before}</span> <span class="text-sc-ink3">→</span> <span class="text-sc-ink">{change.after}</span></span>
						</li>
					{/each}
				</ul>
			{/if}
			<div class="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2.5">
				<button type="button" class="text-[12.5px] text-sc-ink hover:underline" on:click={() => (showChanges = !showChanges)}>
					{changes.length} unsaved change{changes.length === 1 ? '' : 's'} · {showChanges ? 'hide' : 'review'}
				</button>
				<div class="flex gap-2">
					<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink disabled:opacity-40" disabled={saving} on:click={discard}>Discard</button>
					<button type="button" class="rounded-md bg-sc-ink px-3 py-1 text-[12px] font-medium text-black hover:bg-white disabled:opacity-40" disabled={saving || policyMissing} on:click={save}>{saving ? 'Saving…' : 'Save changes'}</button>
				</div>
			</div>
		</div>
	{/if}
</div>
