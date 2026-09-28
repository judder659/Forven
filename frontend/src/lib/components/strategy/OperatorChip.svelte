<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { dismissable } from '$lib/actions/dismissable';
	import { OPERATOR_LABELS } from '$lib/utils/ruleLabels';

	export let op: string;
	export let disabled = false;

	const dispatch = createEventDispatcher<{ change: string }>();
	const SYMBOLS: Record<string, string> = {
		'<': '<', '<=': '≤', '>': '>', '>=': '≥', '==': '=', '!=': '≠', crosses_above: '↗', crosses_below: '↘',
	};
	let open = false;
</script>

<span class="relative inline-block">
	<button type="button" on:click={() => !disabled && (open = !open)} {disabled} aria-label="operator" aria-expanded={open}
		class="px-1 py-0.5 text-[12px] italic text-[#9a9a9a] underline decoration-[#333] decoration-dotted underline-offset-4 hover:text-white disabled:opacity-40">
		{OPERATOR_LABELS[op] ?? op}
	</button>
	{#if open}
		<div use:dismissable={() => (open = false)} role="listbox" aria-label="choose a comparison"
			class="absolute left-0 top-full z-40 mt-1 w-48 border border-[#333] bg-[#080808] py-1 shadow-2xl shadow-black">
			{#each Object.keys(OPERATOR_LABELS) as key}
				<button type="button" role="option" aria-selected={key === op}
					on:click={() => { dispatch('change', key); open = false; }}
					class="flex w-full items-center justify-between px-2 py-1 text-left text-[12px] hover:bg-[#161616] {key === op ? 'text-white' : 'text-[#aaa]'}">
					<span>{OPERATOR_LABELS[key]}</span><span class="font-mono text-[11px] text-[#555]">{SYMBOLS[key]}</span>
				</button>
			{/each}
		</div>
	{/if}
</span>
