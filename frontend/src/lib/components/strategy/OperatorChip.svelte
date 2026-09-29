<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { anchored } from '$lib/actions/anchored';
	import { dismissable } from '$lib/actions/dismissable';
	import { OPERATOR_LABELS } from '$lib/utils/ruleLabels';

	export let op: string;
	export let disabled = false;

	const dispatch = createEventDispatcher<{ change: string }>();
	const SYMBOLS: Record<string, string> = {
		'<': '<', '<=': '≤', '>': '>', '>=': '≥', '==': '=', '!=': '≠', crosses_above: '↗', crosses_below: '↘',
	};
	let open = false;
	let chip: HTMLButtonElement | undefined;
</script>

<span class="relative inline-block">
	<button type="button" bind:this={chip} on:click={() => !disabled && (open = !open)} {disabled} aria-label="operator" aria-expanded={open}
		class="px-1 py-0.5 text-[12px] italic text-sc-ink2 underline decoration-sc-line2 decoration-dotted underline-offset-4 hover:text-sc-ink disabled:opacity-40">
		{OPERATOR_LABELS[op] ?? op}
	</button>
	{#if open}
		<div use:anchored={chip} use:dismissable={() => (open = false)} role="listbox" aria-label="choose a comparison"
			class="rounded-md fixed z-50 w-48 border border-sc-line2 bg-sc-panel py-1 shadow-2xl shadow-black">
			{#each Object.keys(OPERATOR_LABELS) as key}
				<button type="button" role="option" aria-selected={key === op}
					on:click={() => { dispatch('change', key); open = false; }}
					class="flex w-full items-center justify-between px-2 py-1 text-left text-[12px] hover:bg-sc-raise {key === op ? 'text-sc-ink' : 'text-sc-ink2'}">
					<span>{OPERATOR_LABELS[key]}</span><span class="font-mono text-[11px] text-sc-ink3">{SYMBOLS[key]}</span>
				</button>
			{/each}
		</div>
	{/if}
</span>
