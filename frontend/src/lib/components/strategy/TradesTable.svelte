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
			class="border px-2 py-0.5 text-[10px] uppercase tracking-wider {filter === key ? 'border-white bg-white text-black' : 'border-[#2a2a2a] text-[#777] hover:text-white'}">{label}</button>
	{/each}
	<span class="ml-auto text-[10px] text-[#555]">{shown.length} of {trades.length}</span>
</div>
<div class="max-h-[300px] overflow-y-auto border border-[#161616]">
	<table class="w-full text-left text-[11px]">
		<thead class="sticky top-0 bg-[#080808] text-[9px] uppercase tracking-wider text-[#555]">
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
					class="cursor-pointer border-t border-[#111] hover:bg-[#101010] {selected === t.n ? 'bg-[#151515]' : ''}">
					<td class="px-2 py-1 font-mono text-[#666]">{t.n}{#if t.sample === 'out'}<span class="ml-1 text-[9px] text-[#888]" title="out-of-sample">OOS</span>{/if}</td>
					<td class="px-2 py-1 {t.direction === 'long' ? 'text-emerald-400' : 'text-orange-400'}">{t.direction}</td>
					<td class="px-2 py-1 font-mono text-[#aaa]">{t.entry_time.slice(0, 16).replace('T', ' ')}</td>
					<td class="px-2 py-1 text-[#888]">{REASONS[t.exit_reason] ?? t.exit_reason}</td>
					<td class="px-2 py-1 text-right font-mono text-[#888]">{t.bars_held}</td>
					<td class="px-2 py-1 text-right font-mono {t.pnl_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}">{t.pnl_pct >= 0 ? '+' : ''}{(t.pnl_pct * 100).toFixed(2)}%</td>
				</tr>
			{/each}
			{#if !shown.length}
				<tr><td colspan="6" class="px-2 py-4 text-center text-[#555]">No trades{filter === 'all' ? '' : ' match this filter'}.</td></tr>
			{/if}
		</tbody>
	</table>
</div>
