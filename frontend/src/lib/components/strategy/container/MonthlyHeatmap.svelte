<script lang="ts">
	import type { MonthReturn } from '$lib/utils/strategyContainer/metrics';
	import { CHART } from '$lib/utils/strategyContainer/chart';
	import { fmtPct, monthLabel, signClass } from '$lib/utils/strategyContainer/format';

	/** Month-over-month returns, in-sample months dimmed; a dash where no trade closed. */
	export let months: MonthReturn[] = [];
	/** Start of the out-of-sample slice (ms); months ending before it are in-sample. */
	export let oosStart: number | null = null;

	const NEUTRAL = '#262a31';
	const hex = (value: string) => [1, 3, 5].map((index) => parseInt(value.slice(index, index + 2), 16));
	const mix = (a: string, b: string, t: number) => {
		const pa = hex(a);
		const pb = hex(b);
		return `rgb(${pa.map((value, index) => Math.round(value + (pb[index] - value) * t)).join(',')})`;
	};

	$: flat = (value: number | null) => value === null || Math.abs(value) < 1e-9;
	$: maxAbs = Math.max(8, ...months.map((month) => (month.returnPct === null ? 0 : Math.abs(month.returnPct))));
	$: color = (value: number) => (value >= 0 ? mix(NEUTRAL, CHART.gain, Math.min(1, value / maxAbs)) : mix(NEUTRAL, CHART.loss, Math.min(1, -value / maxAbs)));
	$: byKey = new Map(months.map((month) => [`${month.year}-${month.month}`, month]));
	$: years = [...new Set(months.map((month) => month.year))].sort((a, b) => a - b);
	$: isInSample = (year: number, month: number) => oosStart !== null && Date.UTC(year, month + 1, 1) <= oosStart;
	$: yearTotal = (year: number) => {
		let growth = 1;
		let any = false;
		for (const month of months) {
			if (month.year !== year || flat(month.returnPct)) continue;
			growth *= 1 + (month.returnPct as number) / 100;
			any = true;
		}
		return any ? (growth - 1) * 100 : null;
	};
</script>

{#if months.length}
	<div class="grid gap-2" data-testid="monthly-heatmap">
		<div class="flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[11px] text-sc-ink2">
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5" style={`background:${CHART.loss}`}></i>−{maxAbs.toFixed(0)}%</span>
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5" style={`background:${NEUTRAL}`}></i>0</span>
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5" style={`background:${CHART.gain}`}></i>+{maxAbs.toFixed(0)}%</span>
			{#if oosStart !== null}<span class="text-sc-ink3">Dimmed: in-sample</span>{/if}
		</div>
		<div class="overflow-x-auto">
			<table class="w-full min-w-[760px] border-separate border-spacing-[2px] text-[11px] font-plex-mono tabular-nums">
				<thead>
					<tr class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3">
						<th class="w-12 px-1 py-1 text-left font-normal"></th>
						{#each Array.from({ length: 12 }, (_, index) => index) as month (month)}<th class="px-1 py-1 text-center font-normal">{monthLabel(month)}</th>{/each}
						<th class="px-1 py-1 text-right font-normal">Year</th>
					</tr>
				</thead>
				<tbody>
					{#each years as year (year)}
						{@const total = yearTotal(year)}
						<tr>
							<th class="px-1 py-1 text-left font-normal text-sc-ink2">{year}</th>
							{#each Array.from({ length: 12 }, (_, index) => index) as month (month)}
								{@const cell = byKey.get(`${year}-${month}`)}
								{@const dim = isInSample(year, month)}
								{#if !cell}
									<td class="px-1 py-1"></td>
								{:else if flat(cell.returnPct)}
									<td class={`h-[30px] rounded-[3px] px-1 py-1 text-center text-sc-ink3 ${dim ? 'opacity-60' : ''}`} style={`background:${NEUTRAL}`} title={`${monthLabel(month)} ${year}: no trade closed`}>–</td>
								{:else}
									{@const value = cell.returnPct ?? 0}
									<td
										class={`h-[30px] rounded-[3px] px-1 py-1 text-center ${dim ? 'opacity-60' : ''}`}
										style={`background:${color(value)};color:${Math.abs(value) / maxAbs > 0.55 ? '#fff' : CHART.ink}`}
										title={`${monthLabel(month)} ${year}${dim ? ' (in-sample)' : ''}: ${fmtPct(value)}`}
									>{fmtPct(value, 1)}</td>
								{/if}
							{/each}
							<td class={`px-1 py-1 text-right font-medium ${signClass(total)}`}>{total === null ? '—' : fmtPct(total, 1)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</div>
{:else}
	<div class="text-[12px] text-sc-ink3">This run stored no equity curve.</div>
{/if}
