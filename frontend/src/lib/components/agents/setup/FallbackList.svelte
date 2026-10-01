<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { ForvenAgentModelOption } from '$lib/api';
	import { providerLabel } from '$lib/utils/agentsHub/agents';
	import { providerOfKey } from '$lib/utils/agentsHub/routing';
	import ModelSelect from './ModelSelect.svelte';

	/** Ordered fallback keys. */
	export let fallbacks: string[] = [];
	/** The slot's own model, so a fallback that repeats it can be called out. */
	export let primary = '';
	export let options: ForvenAgentModelOption[] = [];
	export let shortlist: Set<string> = new Set();
	export let ariaLabel = 'Fallback';
	export let disabled = false;

	const dispatch = createEventDispatcher<{ change: { fallbacks: string[] } }>();

	$: known = new Map(options.map((option) => [option.key, option]));
	$: listedProviders = new Set(options.map((option) => String(option.provider)));

	function name(key: string): string {
		const option = known.get(key);
		const model = option?.model_id || key.slice(key.indexOf(':') + 1);
		return `${model} · ${providerLabel(providerOfKey(key))}`;
	}

	function problem(key: string, providers: Set<string>): string {
		return providers.has(providerOfKey(key)) ? 'not in the provider’s model list' : 'provider not connected';
	}

	function emit(next: string[]) {
		dispatch('change', { fallbacks: next });
	}

	function add(key: string) {
		if (!key || fallbacks.includes(key)) return;
		emit([...fallbacks, key]);
	}

	function move(index: number, step: -1 | 1) {
		const target = index + step;
		if (target < 0 || target >= fallbacks.length) return;
		const next = [...fallbacks];
		[next[index], next[target]] = [next[target], next[index]];
		emit(next);
	}

	// Re-created after every pick so the add control returns to its placeholder.
	let addNonce = 0;
	function onAdd(event: CustomEvent<{ value: string }>) {
		add(event.detail.value);
		addNonce += 1;
	}
</script>

<div class="grid gap-1">
	{#if fallbacks.length > 0}
		<ol class="m-0 grid list-none gap-1 p-0">
			{#each fallbacks as key, index (key)}
				<li class="flex min-w-0 items-center gap-1.5 rounded border border-sc-line bg-sc-panel2 px-2 py-0.5 text-[11.5px]">
					<span class="w-3 shrink-0 text-center font-plex-mono text-[10.5px] text-sc-ink4">{index + 1}</span>
					<span class={`min-w-0 flex-1 truncate font-plex-mono ${known.has(key) ? 'text-sc-ink2' : 'text-[#e7b24a]'}`} title={known.has(key) ? name(key) : `${name(key)}: ${problem(key, listedProviders)}`}>
						{name(key)}
					</span>
					{#if key === primary}<span class="shrink-0 rounded bg-[#e7b24a]/12 px-1 text-[10px] text-[#e7b24a]" title="The same model as the primary: it adds nothing if that model fails">same</span>{/if}
					<button type="button" class="px-0.5 text-sc-ink3 hover:text-sc-ink disabled:opacity-30" aria-label={`Move ${name(key)} up`} disabled={disabled || index === 0} on:click={() => move(index, -1)}>↑</button>
					<button type="button" class="px-0.5 text-sc-ink3 hover:text-sc-ink disabled:opacity-30" aria-label={`Move ${name(key)} down`} disabled={disabled || index === fallbacks.length - 1} on:click={() => move(index, 1)}>↓</button>
					<button type="button" class="px-0.5 text-sc-ink3 hover:text-[#f2956f] disabled:opacity-30" aria-label={`Remove ${name(key)}`} {disabled} on:click={() => emit(fallbacks.filter((item) => item !== key))}>×</button>
				</li>
			{/each}
		</ol>
	{/if}
	{#key addNonce}
		<ModelSelect value="" options={options.filter((option) => !fallbacks.includes(option.key))} {shortlist} unsetLabel={fallbacks.length ? '+ Add another fallback' : '+ Add a fallback'} {ariaLabel} {disabled} compact on:change={onAdd} />
	{/key}
</div>
