<script lang="ts">
	import type { HeldBack } from '$lib/utils/strategyContainer/evidence';
	import { fmtDateUtc, fmtFraction, fmtNum, fmtPct, isNum, signClass, toNumber } from '$lib/utils/strategyContainer/format';
	import StatGrid from './StatGrid.svelte';

	/** The one-shot test on data sealed from research. */
	export let held: HeldBack | null = null;

	$: m = held?.metrics ?? {};
	$: trades = toNumber(m.total_trades ?? m.trades);
	$: stats = [
		{ label: 'Return', value: fmtFraction(toNumber(m.total_return_pct)), cls: signClass(toNumber(m.total_return_pct)) },
		{ label: 'CAGR', value: fmtFraction(toNumber(m.annualized_return_pct)), cls: signClass(toNumber(m.annualized_return_pct)) },
		{ label: 'Sharpe', value: fmtNum(toNumber(m.sharpe)) },
		{ label: 'Max drawdown', value: fmtFraction(toNumber(m.max_drawdown_pct), 1, false) },
		{ label: 'Trades', value: isNum(trades) ? String(Math.round(trades)) : '—' },
		{ label: 'Profit factor', value: fmtNum(toNumber(m.profit_factor)) },
	];
	$: base = held?.baseline ?? null;
	$: verdictCls = held?.verdict === 'PASS' ? 'border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]' : held?.verdict === 'FAIL' ? 'border-[#e5574f]/45 bg-[#e5574f]/10 text-[#e5574f]' : 'border-[#333] text-[#aab1bc]';
	$: verdictText = held?.verdict === 'PASS' ? '✓ Pass' : held?.verdict === 'FAIL' ? '× Fail' : held?.state === 'exempt' ? 'Exempt' : held?.state === 'running' ? 'Running' : 'Not run';
</script>

<div class="grid gap-3" data-testid="held-back-card">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<div class="text-[11px] text-[#777]">
			{#if held?.start}
				{fmtDateUtc(held.start)} – {fmtDateUtc(held.end)}{isNum(held.bars) ? ` · ${held.bars.toLocaleString('en-US')} bars sealed from research` : ''}{isNum(held.shot) ? ` · shot ${held.shot}${isNum(held.maxShots) ? ` of ${held.maxShots}` : ''}${held.family ? ` for the ${held.family} family` : ''}` : ''}
			{:else if held?.state === 'exempt'}
				Exempt: this strategy predates the held-back rule.
			{:else}
				Runs once, automatically, as the last check before paper.
			{/if}
		</div>
		<span class={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-[12px] font-semibold ${verdictCls}`} data-testid="held-back-verdict">{verdictText}</span>
	</div>
	{#if held?.start}
		<StatGrid items={stats} testid="held-back-stats" />
		{#if base}
			<div class="overflow-x-auto">
				<table class="w-full border-collapse text-[12px] tabular-nums">
					<thead>
						<tr class="text-[10px] uppercase tracking-[0.12em] text-[#555]">
							<th class="px-2.5 py-1.5 text-left font-normal">Same days</th>
							<th class="px-2.5 py-1.5 text-right font-normal">Return</th>
							<th class="px-2.5 py-1.5 text-right font-normal">Sharpe</th>
						</tr>
					</thead>
					<tbody>
						{#each [['This strategy', base.returns.strategy, base.sharpe.strategy], ['Buy & hold', base.returns.buyHold, base.sharpe.buyHold], ['Trend rule', base.returns.trend, base.sharpe.trend]] as [label, ret, sharpe] (label)}
							<tr class="border-t border-[#161616]">
								<td class="px-2.5 py-1.5 text-left text-[#aab1bc]">{label}</td>
								<td class={`px-2.5 py-1.5 text-right ${signClass(Number(ret))}`}>{fmtPct(ret === null ? null : Number(ret))}</td>
								<td class="px-2.5 py-1.5 text-right text-[#ddd]">{fmtNum(sharpe === null ? null : Number(sharpe))}</td>
							</tr>
						{/each}
						<tr class="border-t border-[#161616]">
							<td class="px-2.5 py-1.5 text-left text-[#777]">Alpha a year (t)</td>
							<td class="px-2.5 py-1.5 text-right text-[#ddd]">{fmtPct(base.alphaPct)}</td>
							<td class="px-2.5 py-1.5 text-right text-[#ddd]">t {fmtNum(base.alphaT)}</td>
						</tr>
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</div>
