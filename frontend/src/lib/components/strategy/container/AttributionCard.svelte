<script lang="ts">
	import type { ExitGroup, RegimeSlice, SideSlice, TradeRow } from '$lib/utils/strategyContainer/metrics';
	import { profitConcentration } from '$lib/utils/strategyContainer/metrics';
	import { fmtMonthYear, fmtNum, fmtPct, fmtUsd } from '$lib/utils/strategyContainer/format';
	import SignedBars from './SignedBars.svelte';
	import ConcentrationChart from './ConcentrationChart.svelte';

	/** Out-of-sample attribution of the reference run. */
	export let sides: SideSlice[] = [];
	export let regimes: RegimeSlice[] = [];
	export let exits: ExitGroup[] = [];
	export let trades: TradeRow[] = [];
	export let start: string | null = null;
	export let end: string | null = null;

	$: concentration = profitConcentration(trades);
	$: sideRows = sides.map(({ side, slice }) => ({
		label: side === 'long' ? 'Long' : 'Short',
		sub: `${slice.trades ?? 0} trades · PF ${fmtNum(slice.profitFactor)}`,
		value: slice.totalReturn === null ? null : slice.totalReturn * 100,
	}));
	$: regimeRows = regimes.map(({ label, slice }) => ({
		label,
		sub: `${slice.trades ?? 0} trades · PF ${fmtNum(slice.profitFactor)}`,
		value: slice.totalReturn === null ? null : slice.totalReturn * 100,
	}));
	$: exitRows = exits.map((group) => ({
		label: group.label,
		sub: `${group.count} trades${group.avgReturnPct === null ? '' : ` · avg ${fmtPct(group.avgReturnPct, 2)}`}`,
		value: group.pnl,
	}));
</script>

<article class="grid content-start gap-3 rounded-md border border-sc-line bg-sc-panel px-4 py-3.5" id="attribution" data-testid="attribution-card">
	<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
		<h2 class="m-0 text-[14px] font-semibold text-sc-ink">Where the return comes from</h2>
		<span class="text-[12px] text-sc-ink3">Out-of-sample trades of the run above{start && end ? ` (${fmtMonthYear(start)} – ${fmtMonthYear(end)})` : ''}.</span>
	</div>
	{#if trades.length === 0 && sides.length === 0}
		<div class="text-[12px] text-sc-ink3">This run stored no out-of-sample trades.</div>
	{:else}
		<div class="grid gap-5 md:grid-cols-2 2xl:grid-cols-4">
			<div class="grid content-start gap-2.5">
				<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Long vs short</div><div class="text-[12px] text-sc-ink3">Return contributed by each side</div></div>
				{#if sideRows.length}<SignedBars rows={sideRows} format={(value) => fmtPct(value)} testid="attribution-sides" />{:else}<div class="text-[12px] text-sc-ink3">Not stored for this run.</div>{/if}
			</div>
			<div class="grid content-start gap-2.5">
				<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">By regime at entry</div><div class="text-[12px] text-sc-ink3">Return by market regime</div></div>
				{#if regimeRows.length}<SignedBars rows={regimeRows} format={(value) => fmtPct(value)} testid="attribution-regimes" />{:else}<div class="text-[12px] text-sc-ink3">Not stored for this run.</div>{/if}
			</div>
			<div class="grid content-start gap-2.5">
				<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">By exit</div><div class="text-[12px] text-sc-ink3">Net PnL by how trades closed</div></div>
				{#if exitRows.length}<SignedBars rows={exitRows} format={(value) => fmtUsd(value, 0)} testid="attribution-exits" />{:else}<div class="text-[12px] text-sc-ink3">Trades not stored for this run.</div>{/if}
			</div>
			<div class="grid content-start gap-2.5">
				<div>
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Concentration</div>
					<div class="text-[12px] text-sc-ink3" data-testid="attribution-concentration">
						{concentration ? `Top ${concentration.top} trades = ${fmtPct(concentration.share, 0, false)} of net profit` : trades.length ? 'Net profit is not positive; nothing to concentrate' : 'Trades not stored for this run'}
					</div>
				</div>
				{#if trades.length}<ConcentrationChart rows={trades} />{/if}
			</div>
		</div>
		{#if sides.length === 0 && trades.length > 0}
			<p class="m-0 text-[12px] text-sc-ink3">Side and regime splits are stored with newer runs; the exit mix and concentration come from this run's {trades.length} trades.</p>
		{/if}
	{/if}
</article>
