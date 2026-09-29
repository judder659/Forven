<script lang="ts">
	import type { TradeRow } from '$lib/utils/strategyContainer/metrics';
	import { CHART, linearScale, linePath, trackWidth } from '$lib/utils/strategyContainer/chart';
	import { fmtUsd, fmtUtcStamp } from '$lib/utils/strategyContainer/format';

	/** Trades sorted best to worst as bars, with the running total as a line. */
	export let rows: TradeRow[] = [];
	export let top = 5;
	export let height = 150;

	let width = 320;
	let hover: number | null = null;
	const M = { t: 10, r: 8, b: 20, l: 8 };

	$: sorted = [...rows].sort((a, b) => b.pnl - a.pnl);
	$: cumulative = (() => {
		let total = 0;
		return sorted.map((row) => (total += row.pnl));
	})();
	$: w = Math.max(120, width) - M.l - M.r;
	$: h = height - M.t - M.b;
	$: band = sorted.length ? w / sorted.length : w;
	$: hiY = Math.max(0, ...sorted.map((row) => row.pnl), ...cumulative);
	$: loY = Math.min(0, ...sorted.map((row) => row.pnl), ...cumulative);
	$: y = linearScale(loY, hiY || 1, M.t + h, M.t);
	$: line = linePath(cumulative.map((value, index) => [M.l + (index + 0.5) * band, y(value)]));
	$: topX = M.l + Math.min(top, sorted.length) * band;
	$: topSum = cumulative[Math.min(top, sorted.length) - 1] ?? 0;
	$: net = cumulative[cumulative.length - 1] ?? 0;
	$: hovered = hover === null ? null : sorted[hover] ?? null;

	function move(event: PointerEvent) {
		const svg = (event.currentTarget as SVGElement).ownerSVGElement;
		if (!svg || sorted.length === 0) return;
		const rect = svg.getBoundingClientRect();
		const px = ((event.clientX - rect.left) / (rect.width || 1)) * Math.max(120, width);
		hover = Math.max(0, Math.min(sorted.length - 1, Math.floor((px - M.l) / band)));
	}
</script>

{#if sorted.length > 1}
	<div class="relative" use:trackWidth={(value) => (width = value || 320)} data-testid="concentration-chart">
		<svg viewBox={`0 0 ${Math.max(120, width)} ${height}`} {height} class="block w-full overflow-visible text-[10.5px]" role="img" aria-label={`Trades sorted by profit; the best ${top} made ${fmtUsd(topSum, 0, false)} of ${fmtUsd(net, 0, false)} net`}>
			<line x1={M.l} x2={M.l + w} y1={y(0)} y2={y(0)} stroke={CHART.axis} shape-rendering="crispEdges" />
			{#each sorted as row, index (row.index)}
				<rect
					x={M.l + index * band + 0.3}
					width={Math.max(0.6, band - 0.6)}
					y={Math.min(y(0), y(row.pnl))}
					height={Math.max(0.5, Math.abs(y(row.pnl) - y(0)))}
					fill={row.pnl >= 0 ? CHART.gain : CHART.loss}
					opacity={hover === null || hover === index ? 1 : 0.55}
				/>
			{/each}
			<path d={line} fill="none" stroke={CHART.ink} stroke-width="1.5" stroke-linejoin="round" />
			<line x1={topX} x2={topX} y1={M.t} y2={M.t + h} stroke={CHART.ink3} />
			<text x={topX + 4} y={M.t + 10} fill={CHART.ink2}>top {Math.min(top, sorted.length)}: {fmtUsd(topSum, 0, false)}</text>
			<text x={M.l + w} y={Math.max(M.t + 10, y(net) - 6)} text-anchor="end" fill={CHART.ink2}>net {fmtUsd(net, 0, false)}</text>
			<text x={M.l} y={M.t + h + 14} fill={CHART.ink3}>best trade</text>
			<text x={M.l + w} y={M.t + h + 14} text-anchor="end" fill={CHART.ink3}>worst trade</text>
			<rect x={M.l} y={M.t} width={w} height={h} fill="transparent" role="presentation" on:pointermove={move} on:pointerleave={() => (hover = null)} />
		</svg>
		{#if hovered && hover !== null}
			<div class="pointer-events-none absolute top-0 z-10 min-w-[150px] border border-[#2a2f38] bg-[#0b0d11] px-2.5 py-1.5 text-[11px] shadow-[0_8px_24px_rgba(0,0,0,0.45)]" style={`left:${Math.min(M.l + hover * band + 10, Math.max(0, width - 170))}px`}>
				<div class="text-[#777]">Trade #{hovered.index} · {hovered.side} · closed {fmtUtcStamp(hovered.exit)}</div>
				<div class="flex justify-between gap-3"><span class="text-[#777]">PnL</span><span class="text-white">{fmtUsd(hovered.pnl, 0)}</span></div>
				<div class="flex justify-between gap-3"><span class="text-[#777]">Running total</span><span class="text-white">{fmtUsd(cumulative[hover], 0)}</span></div>
			</div>
		{/if}
	</div>
{:else}
	<div class="text-[12px] text-[#666]">Too few trades to rank.</div>
{/if}
