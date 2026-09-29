<script lang="ts">
	import { CHART, linearScale, linePath, niceTicks, trackWidth } from '$lib/utils/strategyContainer/chart';
	import { fmtPct } from '$lib/utils/strategyContainer/format';

	/**
	 * Where paper should land after N trades: percentiles of resampled trade paths
	 * ([step, p5, p25, p50, p75, p95] in equity dollars), with paper's own closed-trade
	 * equity drawn on top by trade number.
	 */
	export let fan: Array<[number, number, number, number, number, number]> = [];
	/** Paper equity after each closed trade, starting at the book's base. */
	export let paper: number[] = [];
	export let height = 230;
	export let emptyText = 'Run the Monte Carlo test to see where paper should land.';

	let width = 560;
	let cursor: number | null = null;
	const M = { t: 16, r: 78, b: 26, l: 48 };

	$: base = fan[0]?.[3] ?? 1;
	$: paperBase = paper[0] ?? base;
	$: steps = fan.length ? fan[fan.length - 1][0] : 0;
	$: w = Math.max(200, width) - M.l - M.r;
	$: h = height - M.t - M.b;
	$: x = linearScale(0, Math.max(1, steps), M.l, M.l + w);
	$: pct = (value: number) => (value / base - 1) * 100;
	$: paperPct = paper.slice(0, steps + 1).map((value) => (value / paperBase - 1) * 100);
	$: lo = Math.min(...fan.map((row) => pct(row[1])), ...paperPct, 0);
	$: hi = Math.max(...fan.map((row) => pct(row[5])), ...paperPct, 0);
	$: pad = (hi - lo) * 0.06 || 1;
	$: y = linearScale(lo - pad, hi + pad, M.t + h, M.t);
	$: yTicks = niceTicks(lo, hi, 5);
	$: xTicks = niceTicks(0, steps, 5).filter((tick) => Number.isInteger(tick));
	$: band = (a: number, b: number) =>
		`${linePath(fan.map((row) => [x(row[0]), y(pct(row[a]))]))}${[...fan].reverse().map((row) => `L${x(row[0]).toFixed(1)},${y(pct(row[b])).toFixed(1)}`).join('')}Z`;
	$: median = linePath(fan.map((row) => [x(row[0]), y(pct(row[3]))]));
	$: paperPath = paperPct.length > 1 ? linePath(paperPct.map((value, index) => [x(index), y(value)])) : '';
	$: last = fan[fan.length - 1];
	$: row = cursor === null ? null : fan[Math.max(0, Math.min(fan.length - 1, cursor))] ?? null;
	$: closed = Math.max(0, paper.length - 1);

	function move(event: PointerEvent) {
		const svg = (event.currentTarget as SVGElement).ownerSVGElement;
		if (!svg || fan.length === 0) return;
		const rect = svg.getBoundingClientRect();
		const px = ((event.clientX - rect.left) / (rect.width || 1)) * Math.max(200, width);
		cursor = Math.round(Math.max(0, Math.min(steps, x.invert(px))));
	}
</script>

{#if fan.length > 1}
	<div class="grid gap-2" data-testid="forward-cone">
		<div class="flex flex-wrap gap-x-3.5 gap-y-1 text-[11px] text-[#aab1bc]">
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5 bg-[rgba(238,241,245,0.12)]"></i>Middle 50%</span>
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5 bg-[rgba(238,241,245,0.07)]"></i>Middle 90%</span>
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0.5 w-4 bg-[#aab1bc]"></i>Median</span>
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0.5 w-4 bg-[#eef1f5]"></i>Paper</span>
		</div>
		<div class="relative" use:trackWidth={(value) => (width = value || 560)}>
			<svg viewBox={`0 0 ${Math.max(200, width)} ${height}`} {height} class="block w-full overflow-visible text-[10.5px]" role="img" aria-label={`Resampled paths for the next ${steps} trades; paper has closed ${closed}`}>
				{#each yTicks as tick (tick)}
					<line x1={M.l} x2={M.l + w} y1={y(tick)} y2={y(tick)} stroke={tick === 0 ? CHART.axis : CHART.grid} shape-rendering="crispEdges" />
					<text x={M.l - 8} y={y(tick) + 3.5} text-anchor="end" fill={CHART.ink3}>{fmtPct(tick, 0)}</text>
				{/each}
				{#each xTicks as tick (tick)}
					<text x={x(tick)} y={M.t + h + 16} text-anchor="middle" fill={CHART.ink3}>{tick === 0 ? 'trade 0' : tick}</text>
				{/each}
				<path d={band(1, 5)} fill="rgba(238,241,245,0.07)" />
				<path d={band(2, 4)} fill="rgba(238,241,245,0.12)" />
				<path d={median} fill="none" stroke={CHART.ink2} stroke-width="1.5" />
				{#if last}
					<text x={x(last[0]) + 6} y={y(pct(last[5])) + 4} fill={CHART.ink3}>p95 {fmtPct(pct(last[5]), 0)}</text>
					<text x={x(last[0]) + 6} y={y(pct(last[3])) + 4} fill={CHART.ink2}>median {fmtPct(pct(last[3]), 0)}</text>
					<text x={x(last[0]) + 6} y={y(pct(last[1])) + 4} fill={CHART.ink3}>p5 {fmtPct(pct(last[1]), 0)}</text>
				{/if}
				{#if paperPath}<path d={paperPath} fill="none" stroke={CHART.ink} stroke-width="2" stroke-linejoin="round" />{/if}
				<circle cx={x(Math.min(closed, steps))} cy={y(paperPct[Math.min(closed, steps)] ?? 0)} r="4.5" fill={CHART.ink} stroke={CHART.panel} stroke-width="2" />
				<text x={x(Math.min(closed, steps)) + 8} y={y(paperPct[Math.min(closed, steps)] ?? 0) - 8} fill={CHART.ink}>paper: {closed} trade{closed === 1 ? '' : 's'}{closed ? ` · ${fmtPct(paperPct[Math.min(closed, steps)] ?? 0)}` : ' so far'}</text>
				{#if cursor !== null}
					<line x1={x(cursor)} x2={x(cursor)} y1={M.t} y2={M.t + h} stroke={CHART.ink3} />
				{/if}
				<rect x={M.l} y={M.t} width={w} height={h} fill="transparent" role="presentation" on:pointermove={move} on:pointerleave={() => (cursor = null)} />
			</svg>
			{#if row && cursor !== null}
				<div class="pointer-events-none absolute top-2 z-10 min-w-[150px] border border-[#2a2f38] bg-[#0b0d11] px-2.5 py-1.5 text-[11px] shadow-[0_8px_24px_rgba(0,0,0,0.45)]" style={`left:${Math.min(x(cursor) + 12, Math.max(0, width - 170))}px`} data-testid="forward-cone-readout">
					<div class="mb-0.5 text-[#777]">after {cursor} trade{cursor === 1 ? '' : 's'}</div>
					{#each [['95th pct', 5], ['75th pct', 4], ['Median', 3], ['25th pct', 2], ['5th pct', 1]] as [name, index] (name)}
						<div class="flex justify-between gap-3"><span class="text-[#777]">{name}</span><span class="tabular-nums text-white">{fmtPct(pct(row[Number(index)]))}</span></div>
					{/each}
					{#if cursor <= closed && paperPct[cursor] !== undefined}
						<div class="flex justify-between gap-3 border-t border-[#2a2f38] pt-0.5"><span class="text-[#777]">Paper</span><span class="tabular-nums text-white">{fmtPct(paperPct[cursor])}</span></div>
					{/if}
				</div>
			{/if}
		</div>
	</div>
{:else}
	<div class="text-[12px] text-[#666]" data-testid="forward-cone-empty">{emptyText}</div>
{/if}
