<script lang="ts">
	// The preview's trades, newest first; picking one selects it on the chart.
	import { createEventDispatcher } from 'svelte';
	import type { PreviewTrade } from '$lib/api';

	export let trades: PreviewTrade[] = [];
	export let selected: number | null = null;

	const dispatch = createEventDispatcher<{ select: number }>();
	type Filter = 'all' | 'out' | 'win' | 'loss';
	let filter: Filter = 'all';
	const FILTERS: [Filter, string][] = [['all', 'All'], ['out', 'Out-of-sample'], ['win', 'Winners'], ['loss', 'Losers']];
	const REASONS: Record<string, string> = {
		signal: 'rule', stop_loss: 'stop', take_profit: 'target', trailing_stop: 'trail',
		time_stop: 'time', liquidation: 'liquidated', window_end: 'window end',
	};

	$: shown = trades
		.filter((t) => filter === 'all' || (filter === 'out' ? t.sample === 'out' : filter === 'win' ? t.pnl_pct > 0 : t.pnl_pct <= 0))
		.slice()
		.reverse();
</script>

<div class="flex items-center gap-1 pb-2">
	{#each FILTERS as [key, label]}
		<button type="button" on:click={() => (filter = key)}
			class="rounded-md border px-2 py-0.5 text-[12px] {filter === key ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink3 hover:text-sc-ink'}">{label}</button>
	{/each}
	<span class="ml-auto text-[10px] text-sc-ink3">{shown.length} of {trades.length}</span>
</div>
<div class="max-h-[300px] overflow-y-auto border border-sc-line">
	<table class="w-full text-left text-[11px]">
		<thead class="sticky top-0 bg-sc-panel font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
			<tr>
				<th class="px-2 py-1 font-normal">#</th>
				<th class="px-2 py-1 font-normal">Side</th>
				<th class="px-2 py-1 font-normal">Entry</th>
				<th class="px-2 py-1 font-normal">Exit</th>
				<th class="px-2 py-1 text-right font-normal">Bars</th>
				<th class="px-2 py-1 text-right font-normal">P&amp;L</th>
			</tr>
		</thead>
		<tbody>
			{#each shown as t (t.n)}
				<tr on:click={() => dispatch('select', t.n)}
					class="cursor-pointer border-t border-sc-line hover:bg-sc-panel2 {selected === t.n ? 'bg-sc-raise' : ''}">
					<td class="px-2 py-1 font-mono text-sc-ink3">{t.n}{#if t.sample === 'out'}<span class="ml-1 text-[9px] text-sc-ink2" title="out-of-sample">OOS</span>{/if}</td>
					<td class="px-2 py-1 {t.direction === 'long' ? 'text-emerald-400' : 'text-orange-400'}">{t.direction}</td>
					<td class="px-2 py-1 font-mono text-sc-ink2">{t.entry_time.slice(0, 16).replace('T', ' ')}</td>
					<td class="px-2 py-1 text-sc-ink2">{REASONS[t.exit_reason] ?? t.exit_reason}</td>
					<td class="px-2 py-1 text-right font-mono text-sc-ink2">{t.bars_held}</td>
					<td class="px-2 py-1 text-right font-mono {t.pnl_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}">{t.pnl_pct >= 0 ? '+' : ''}{(t.pnl_pct * 100).toFixed(2)}%</td>
				</tr>
			{/each}
			{#if !shown.length}
				<tr><td colspan="6" class="px-2 py-4 text-center text-sc-ink3">No trades{filter === 'all' ? '' : ' match this filter'}.</td></tr>
			{/if}
		</tbody>
	</table>
</div>
