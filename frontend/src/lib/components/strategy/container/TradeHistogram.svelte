<script lang="ts">
	import type { TradeRow } from '$lib/utils/strategyContainer/metrics';
	import { CHART, linearScale, niceTicks, trackWidth } from '$lib/utils/strategyContainer/chart';
	import { fmtPct } from '$lib/utils/strategyContainer/format';

	/** Distribution of per-trade returns (percent of equity), gains teal and losses red-orange. */
	export let rows: TradeRow[] = [];
	export let height = 200;

	let width = 420;
	let hover: number | null = null;
	const M = { t: 16, r: 12, b: 26, l: 34 };

	$: returns = rows.map((row) => row.returnPct).filter((value): value is number => value !== null && Number.isFinite(value));
	$: rawLo = returns.length ? Math.min(...returns) : 0;
	$: rawHi = returns.length ? Math.max(...returns) : 1;
	$: guide = niceTicks(rawLo, rawHi, 14);
	$: step = guide.length > 1 ? guide[1] - guide[0] : 0.5;
	$: lo = Math.floor(rawLo / step) * step;
	// A return on the top edge lands in the last bin (the index is clamped below).
	$: hi = Math.max(lo + step, Math.ceil(rawHi / step) * step);
	$: bins = (() => {
		const out: Array<{ a: number; b: number; n: number }> = [];
		for (let a = lo; a < hi - step * 1e-6; a += step) out.push({ a, b: a + step, n: 0 });
		for (const value of returns) {
			const index = Math.min(out.length - 1, Math.max(0, Math.floor((value - lo) / step)));
			if (out[index]) out[index].n += 1;
		}
		return out;
	})();
	$: w = Math.max(160, width) - M.l - M.r;
	$: h = height - M.t - M.b;
	$: x = linearScale(lo, hi, M.l, M.l + w);
	$: maxN = Math.max(1, ...bins.map((bin) => bin.n));
	$: y = linearScale(0, maxN, M.t + h, M.t);
	$: yTicks = niceTicks(0, maxN, 4).filter((tick) => Number.isInteger(tick));
	$: xTicks = niceTicks(lo, hi, 7);
	$: mean = returns.length ? returns.reduce((sum, value) => sum + value, 0) / returns.length : null;
	$: hovered = hover === null ? null : bins[hover] ?? null;
</script>

{#if returns.length > 1}
	<div class="relative" use:trackWidth={(value) => (width = value || 420)} data-testid="trade-histogram">
		<svg viewBox={`0 0 ${Math.max(160, width)} ${height}`} {height} class="block w-full overflow-visible font-plex-mono text-[10.5px]" role="img" aria-label={`Histogram of ${returns.length} trade returns${mean === null ? '' : `, mean ${fmtPct(mean, 2)}`}`}>
			{#each yTicks as tick (tick)}
				<line x1={M.l} x2={M.l + w} y1={y(tick)} y2={y(tick)} stroke={tick === 0 ? CHART.axis : CHART.grid} shape-rendering="crispEdges" />
				<text x={M.l - 6} y={y(tick) + 3.5} text-anchor="end" fill={CHART.ink3}>{tick}</text>
			{/each}
			{#each xTicks as tick (tick)}
				<text x={x(tick)} y={M.t + h + 16} text-anchor="middle" fill={CHART.ink3}>{fmtPct(tick, step < 1 ? 1 : 0)}</text>
			{/each}
			{#each bins as bin, index (bin.a)}
				{#if bin.n > 0}
					{@const bw = Math.max(1, x(bin.b) - x(bin.a) - 2)}
					<rect
						role="presentation"
						x={x(bin.a) + 1}
						y={y(bin.n)}
						width={bw}
						height={Math.max(0.5, y(0) - y(bin.n))}
						rx={Math.min(3, bw / 2)}
						fill={(bin.a + bin.b) / 2 >= 0 ? CHART.gain : CHART.loss}
						opacity={hover === null || hover === index ? 1 : 0.6}
						on:pointerenter={() => (hover = index)}
						on:pointerleave={() => (hover = null)}
					/>
				{/if}
			{/each}
			{#if mean !== null}
				<line x1={x(mean)} x2={x(mean)} y1={M.t} y2={y(0)} stroke={CHART.ink} stroke-width="1.5" />
				<text x={x(mean) + 5} y={M.t + 8} fill={CHART.ink2}>mean {fmtPct(mean, 2)}</text>
			{/if}
		</svg>
		{#if hovered && hover !== null}
			<div class="pointer-events-none absolute top-0 z-10 rounded-md border border-sc-line2 bg-[#0b0d11] px-2.5 py-1.5 text-[11px] shadow-[0_8px_24px_rgba(0,0,0,0.45)]" style={`left:${Math.min(x(hovered.a) + 8, Math.max(0, width - 150))}px`}>
				<div class="text-sc-ink3">{fmtPct(hovered.a, 1)} to {fmtPct(hovered.b, 1)}</div>
				<div class="flex justify-between gap-3"><span class="text-sc-ink3">Trades</span><span class="text-sc-ink">{hovered.n}</span></div>
			</div>
		{/if}
	</div>
{:else}
	<div class="text-[12px] text-sc-ink3">Too few trades for a distribution.</div>
{/if}
