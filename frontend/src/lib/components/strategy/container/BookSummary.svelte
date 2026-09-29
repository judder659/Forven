<script lang="ts">
	import type { BookStats } from '$lib/utils/strategyContainer/ladder';
	import { PAPER_START_EQUITY } from '$lib/utils/strategyContainer/ladder';
	import { fmtDateUtc, fmtFraction, fmtNum, fmtUsd, signClass } from '$lib/utils/strategyContainer/format';
	import StatGrid from './StatGrid.svelte';

	/** One book's closed trades. Paper and live dollars are never added together. */
	export let book: 'paper' | 'live';
	export let stats: BookStats | null = null;
	export let empty = '';

	$: items = stats
		? [
				{ label: 'Closed trades', value: String(stats.count) },
				{ label: 'Win rate', value: fmtFraction(stats.winRate, 0, false) },
				{ label: 'Realized PnL', value: fmtUsd(stats.pnl), cls: signClass(stats.pnl) },
				{ label: 'Profit factor', value: stats.profitFactor === null ? (stats.wins === stats.count ? '∞' : '—') : fmtNum(stats.profitFactor) },
				...(book === 'paper'
					? [
							{ label: `Return on $${(PAPER_START_EQUITY / 1000).toFixed(0)}k`, value: fmtFraction(stats.totalReturn), cls: signClass(stats.totalReturn) },
							{ label: 'Max drawdown', value: fmtFraction(stats.maxDrawdown, 1, false) },
						]
					: []),
			]
		: [];
</script>

<div class="grid content-start gap-3 border border-[#1d1d1d] bg-[#0b0b0b] p-4" data-testid={`book-summary-${book}`}>
	<div class="flex flex-wrap items-baseline justify-between gap-2">
		<h3 class="m-0 text-[13px] font-semibold text-white">{book === 'paper' ? 'Paper' : 'Live'}</h3>
		<span class={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide ${book === 'live' ? 'border-emerald-700/60 text-emerald-300' : 'border-[#2a2f38] text-[#888]'}`}>{book === 'live' ? 'real money' : `simulated $${(PAPER_START_EQUITY / 1000).toFixed(0)}k book`}</span>
	</div>
	{#if stats}
		<StatGrid {items} />
		{#if stats.firstOpened}<div class="text-[11px] text-[#666]">First trade opened {fmtDateUtc(stats.firstOpened)}{book === 'live' ? ' · live PnL is in real wallet dollars, so there is no fixed base to take a return on' : ''}</div>{/if}
	{:else}
		<p class="m-0 text-[12px] text-[#777]">{empty}</p>
	{/if}
</div>
