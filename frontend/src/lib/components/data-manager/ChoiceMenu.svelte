<script lang="ts">
	// A toolbar button that opens a checkbox list (filter facets, columns).
	import { createEventDispatcher, tick } from 'svelte';
	import { anchored } from '$lib/actions/anchored';
	import { dismissable } from '$lib/actions/dismissable';

	export let label: string;
	export let items: Array<{ value: string; label: string; count?: number; title?: string }> = [];
	export let selected: string[] = [];
	/** Text shown after the label when something is selected (defaults to the selection). */
	export let summary: string | null = null;
	export let clearable = true;

	const dispatch = createEventDispatcher<{ change: string[] }>();
	let open = false;
	let button: HTMLButtonElement | undefined;
	let panel: HTMLDivElement | undefined;

	$: shown = summary ?? (selected.length === 1 ? items.find((i) => i.value === selected[0])?.label ?? selected[0] : selected.length ? String(selected.length) : '');

	async function toggleOpen() {
		open = !open;
		if (open) {
			await tick();
			panel?.querySelector<HTMLInputElement>('input')?.focus();
		}
	}
	function toggle(value: string) {
		dispatch('change', selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value]);
	}
	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape' && open) {
			event.stopPropagation();
			open = false;
			button?.focus();
		}
	}
</script>

<div class="relative" use:dismissable={() => (open = false)} on:keydown={onKey} role="presentation">
	<button bind:this={button} type="button" on:click={toggleOpen} aria-expanded={open} aria-haspopup="true"
		class="flex items-center gap-1 whitespace-nowrap border px-2 py-1 text-[10px] uppercase tracking-wider transition-colors {selected.length ? 'border-[#666] text-white' : 'border-[#2a2a2a] text-[#888] hover:border-[#555] hover:text-white'}">
		{label}{#if shown}<span class="normal-case tracking-normal text-[#ccc]">: {shown}</span>{/if}
		<span class="text-[8px] text-[#666]" aria-hidden="true">▾</span>
	</button>
	{#if open}
		<div bind:this={panel} use:anchored={button} role="group" aria-label={label}
			class="z-50 max-h-80 min-w-[13rem] overflow-y-auto border border-[#333] bg-[#050505] py-1 shadow-[0_12px_32px_rgba(0,0,0,0.7)]">
			{#each items as item (item.value)}
				<label class="flex cursor-pointer items-center gap-2 px-3 py-1 text-[11px] hover:bg-[#111]" title={item.title}>
					<input type="checkbox" checked={selected.includes(item.value)} on:change={() => toggle(item.value)} class="accent-white" />
					<span class="flex-1 text-[#ddd]">{item.label}</span>
					{#if item.count != null}<span class="font-mono text-[10px] tabular-nums text-[#666]">{item.count.toLocaleString('en-US')}</span>{/if}
				</label>
			{:else}
				<div class="px-3 py-1.5 text-[11px] text-[#666]">Nothing to choose.</div>
			{/each}
			{#if clearable && selected.length}
				<button type="button" on:click={() => dispatch('change', [])}
					class="mt-1 w-full border-t border-[#1a1a1a] px-3 pt-1.5 text-left text-[10px] uppercase tracking-wider text-[#888] hover:text-white">Clear</button>
			{/if}
		</div>
	{/if}
</div>
