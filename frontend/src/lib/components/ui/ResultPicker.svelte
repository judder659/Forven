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
	<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{label}</div>
	<select
		id={id}
		bind:value
		class="rounded-md mt-1.5 w-full border border-sc-line2 bg-sc-panel px-3 py-2 text-sm text-sc-ink outline-none transition-colors focus:border-sc-ink"
	>
		<option value="">Select result…</option>
		{#each items as item}
			<option value={item.result_id}>{item.result_id} — {item.symbol} {item.timeframe}</option>
		{/each}
	</select>
	{#if helpText}
		<div class="mt-1 text-[11px] text-sc-ink3">{helpText}</div>
	{/if}

	{#if selectedItem}
		<div class="rounded-md mt-2 border border-sc-line bg-sc-panel px-3 py-2 text-[11px] text-sc-ink2">
			<div class="flex flex-wrap items-center gap-2">
				<span class="rounded-md border border-sc-line2 bg-sc-bg px-2 py-0.5 font-mono text-sc-ink">{selectedItem.result_id}</span>
				<span>{selectedItem.symbol || '--'}</span>
				<span class="text-sc-ink3">/</span>
				<span>{selectedItem.timeframe || '--'}</span>
				<span class="text-sc-ink3">/</span>
				<span>{fmtShortDate(selectedItem.start_date)} -> {fmtShortDate(selectedItem.end_date)}</span>
			</div>
		</div>
	{/if}
</label>
