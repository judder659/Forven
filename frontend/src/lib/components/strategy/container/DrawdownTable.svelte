<script lang="ts">
	import type { DrawdownPeriod } from '$lib/utils/strategyContainer/metrics';
	import { fmtDateUtc, fmtNum, fmtPct } from '$lib/utils/strategyContainer/format';

	/** The deepest peak-to-recovery drawdowns. */
	export let periods: DrawdownPeriod[] = [];
</script>

{#if periods.length}
	<div class="overflow-x-auto" data-testid="drawdown-table">
		<table class="w-full border-collapse text-[12px] tabular-nums">
			<thead>
				<tr class="text-[10px] uppercase tracking-[0.12em] text-[#555]">
					<th class="px-2.5 py-1.5 text-right font-normal">Depth</th>
					<th class="px-2.5 py-1.5 text-left font-normal">Peak</th>
					<th class="px-2.5 py-1.5 text-left font-normal">Trough</th>
					<th class="px-2.5 py-1.5 text-left font-normal">Recovered</th>
					<th class="px-2.5 py-1.5 text-right font-normal">Days</th>
				</tr>
			</thead>
			<tbody>
				{#each periods as period (period.start)}
					<tr class="border-t border-[#161616]">
						<td class="px-2.5 py-1.5 text-right text-[#f2956f]">{fmtPct(period.depthPct, 1)}</td>
						<td class="px-2.5 py-1.5 text-left text-[#aab1bc]">{fmtDateUtc(period.start)}</td>
						<td class="px-2.5 py-1.5 text-left text-[#aab1bc]">{fmtDateUtc(period.trough)}</td>
						<td class="px-2.5 py-1.5 text-left text-[#aab1bc]">{period.recovered === null ? 'still open at run end' : fmtDateUtc(period.recovered)}</td>
						<td class="px-2.5 py-1.5 text-right text-[#ddd]">{fmtNum(period.days, 0)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{:else}
	<div class="text-[12px] text-[#666]">No drawdown in this curve.</div>
{/if}
