<script lang="ts">
	/**
	 * Cumulative realized P&L from closed live trades, net of recorded fees and
	 * funding, over a trailing window. Steps at each close; green above zero,
	 * red below.
	 */
	import { onDestroy, onMount } from 'svelte';
	import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';
	import type { ForvenEquityHistory } from '$lib/api';
	import { cumulativePnlSeries, formatUsd, pnlTone, type PnlPoint } from '$lib/utils/liveDashboard';

	export let history: ForvenEquityHistory | null = null;

	type Range = '7d' | '30d' | 'all';
	const RANGES: Array<{ key: Range; label: string; ms: number | null }> = [
		{ key: '7d', label: '7D', ms: 7 * 86_400_000 },
		{ key: '30d', label: '30D', ms: 30 * 86_400_000 },
		{ key: 'all', label: 'All', ms: null },
	];

	let range: Range = '30d';
	let container: HTMLDivElement;
	let chart: IChartApi | null = null;
	let series: ISeriesApi<'Baseline'> | null = null;
	let observer: ResizeObserver | null = null;

	$: curve = Array.isArray(history?.curve) ? history.curve : [];
	$: hasTrades = curve.some((point) => !point.is_current);
	$: windowMs = RANGES.find((option) => option.key === range)?.ms ?? null;
	$: points = cumulativePnlSeries(curve, Number(history?.base ?? 0), windowMs);
	$: total = points.length > 0 ? points[points.length - 1].value : null;
	$: if (series) render(points);

	function render(data: PnlPoint[]) {
		if (!series) return;
		series.setData(
			data.map((point) => ({
				time: Math.floor(Date.parse(point.timestamp) / 1000) as UTCTimestamp,
				value: Math.round(point.value * 100) / 100,
			})),
		);
		chart?.timeScale().fitContent();
	}

	onMount(async () => {
		const { createChart, ColorType, CrosshairMode, LineType } = await import('lightweight-charts');
		if (!container) return;
		chart = createChart(container, {
			width: container.clientWidth,
			height: 180,
			layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#6b7280', fontSize: 10 },
			grid: { vertLines: { visible: false }, horzLines: { color: '#161616' } },
			rightPriceScale: { borderColor: '#222' },
			timeScale: { borderColor: '#222' },
			crosshair: { mode: CrosshairMode.Magnet },
			localization: { priceFormatter: (value: number) => formatUsd(value, true) },
		});
		series = chart.addBaselineSeries({
			baseValue: { type: 'price', price: 0 },
			lineWidth: 2,
			lineType: LineType.WithSteps,
			topLineColor: '#34d399',
			topFillColor1: 'rgba(52, 211, 153, 0.12)',
			topFillColor2: 'rgba(52, 211, 153, 0.02)',
			bottomLineColor: '#f87171',
			bottomFillColor1: 'rgba(248, 113, 113, 0.02)',
			bottomFillColor2: 'rgba(248, 113, 113, 0.12)',
			priceLineVisible: false,
		});
		render(points);
		observer = new ResizeObserver(() => {
			if (chart && container) chart.applyOptions({ width: container.clientWidth });
		});
		observer.observe(container);
	});

	onDestroy(() => {
		observer?.disconnect();
		observer = null;
		chart?.remove();
		chart = null;
		series = null;
	});
</script>

<div class="border border-[#222] bg-[#050505]" data-testid="live-pnl">
	<div class="flex items-center justify-between gap-2 border-b border-[#222] px-3 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-wider text-gray-400" title="Closed live trades, net of recorded fees and funding">
			Realized P&amp;L
			{#if hasTrades && total !== null}
				<span class="ml-1 font-mono normal-case {pnlTone(total)}">{formatUsd(total, true)}</span>
			{/if}
		</h2>
		<div class="flex items-center" role="group" aria-label="P&L window">
			{#each RANGES as option (option.key)}
				<button
					type="button"
					class="border px-2 py-0.5 text-[10px] uppercase tracking-wider {range === option.key
						? 'border-white bg-white text-black'
						: 'border-[#333] text-gray-500 hover:text-white'}"
					aria-pressed={range === option.key}
					on:click={() => (range = option.key)}
				>
					{option.label}
				</button>
			{/each}
		</div>
	</div>
	<div class="relative px-1 py-1">
		<div bind:this={container} class="h-[180px] w-full"></div>
		{#if !hasTrades}
			<div class="absolute inset-0 flex items-center justify-center bg-[#050505] text-xs text-gray-500">
				No closed live trades yet.
			</div>
		{/if}
	</div>
</div>
