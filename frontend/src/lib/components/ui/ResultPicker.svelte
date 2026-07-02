<script lang="ts">
	import type { StrategyContainerHistoryItem } from '$lib/api';

	export let id = '';
	export let label = 'Gauntlet result';
	export let value = '';
	export let helpText = '';
	export let items: StrategyContainerHistoryItem[] = [];

	function fmtShortDate(value: string | null | undefined): string {
		if (!value) return '--';
		const parsed = new Date(value);
		if (Number.isNaN(parsed.getTime())) return '--';
		return parsed.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
	}

	$: selectedItem = items.find((item) => String(item.result_id || '').trim() === value) ?? null;
</script>

<label class="block" for={id}>
	<div class="text-[10px] uppercase tracking-[0.2em] text-[#666]">{label}</div>
	<select
		id={id}
		bind:value
		class="mt-1.5 w-full border border-[#333] bg-[#050505] px-3 py-2 text-sm text-white outline-none transition-colors focus:border-white"
	>
		<option value="">Select result…</option>
		{#each items as item}
			<option value={item.result_id}>{item.result_id} — {item.symbol} {item.timeframe}</option>
		{/each}
	</select>
	{#if helpText}
		<div class="mt-1 text-[11px] text-[#666]">{helpText}</div>
	{/if}

	{#if selectedItem}
		<div class="mt-2 border border-[#1a1a1a] bg-[#070707] px-3 py-2 text-[11px] text-[#888]">
			<div class="flex flex-wrap items-center gap-2">
				<span class="border border-[#333] bg-black px-2 py-0.5 font-mono text-white">{selectedItem.result_id}</span>
				<span>{selectedItem.symbol || '--'}</span>
				<span class="text-[#555]">/</span>
				<span>{selectedItem.timeframe || '--'}</span>
				<span class="text-[#555]">/</span>
				<span>{fmtShortDate(selectedItem.start_date)} -> {fmtShortDate(selectedItem.end_date)}</span>
			</div>
		</div>
	{/if}
</label>
