<script lang="ts" context="module">
	export interface EquityHistory {
		base: number;
		curve: Array<{ time: string | number; value: number }>;
	}
</script>

<script lang="ts">
	/** An equity or P&L line shaded above/below where it started: the live account, or one strategy. */
	import { onDestroy } from 'svelte';
	import { createChart, type IChartApi, type IPriceLine, type ISeriesApi, type Time } from 'lightweight-charts';
	import { parseTs } from '$lib/utils/tradingDesk/format';

	export let history: EquityHistory | null = null;
	export let label = 'Live account equity after each closed trade';
	export let emptyText = 'No closed live trades yet.';
	export let startTitle = 'Start';

	let container: HTMLDivElement;
	let chart: IChartApi | null = null;
	let series: ISeriesApi<'Baseline'> | null = null;
	let startLine: IPriceLine | null = null;
	let drawnCount = 0;

	function pointsOf(value: EquityHistory | null): Array<{ time: Time; value: number }> {
		const points: Array<{ time: Time; value: number }> = [];
		for (const point of value?.curve ?? []) {
			const ms = parseTs(point.time);
			if (ms === null || !Number.isFinite(point.value)) continue;
			const time = Math.floor(ms / 1000);
			const last = points[points.length - 1];
			if (last && (last.time as number) >= time) last.value = point.value;
			else points.push({ time: time as Time, value: point.value });
		}
		return points;
	}

	function render(value: EquityHistory | null): void {
		if (!container) return;
		const points = pointsOf(value);
		const base = value?.base;
		if (points.length < 2 || typeof base !== 'number') {
			chart?.remove();
			chart = null;
			series = null;
			startLine = null;
			drawnCount = 0;
			return;
		}
		if (!chart) {
			chart = createChart(container, {
				autoSize: true,
				layout: { background: { color: 'transparent' }, textColor: '#747c88', fontFamily: '"IBM Plex Mono", ui-monospace, monospace', fontSize: 10 },
				grid: { vertLines: { visible: false }, horzLines: { color: '#12151a' } },
				rightPriceScale: { borderColor: '#1c2026' },
				timeScale: { borderColor: '#1c2026' },
				handleScroll: false,
				handleScale: false,
			});
			series = chart.addBaselineSeries({
				topLineColor: '#139a9f',
				topFillColor1: 'rgba(19,154,159,0.22)',
				topFillColor2: 'rgba(19,154,159,0.02)',
				bottomLineColor: '#e0663f',
				bottomFillColor1: 'rgba(224,102,63,0.02)',
				bottomFillColor2: 'rgba(224,102,63,0.22)',
				lineWidth: 2,
				priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
			});
		}
		if (!series) return;
		series.applyOptions({ baseValue: { type: 'price', price: base } });
		series.setData(points);
		if (startLine) series.removePriceLine(startLine);
		startLine = series.createPriceLine({ price: base, color: '#4b525c', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: startTitle });
		// Refit when a trade adds a point; the moving "now" point alone should not jump the view.
		if (points.length !== drawnCount) chart?.timeScale().fitContent();
		drawnCount = points.length;
	}

	$: if (container) render(history);
	onDestroy(() => chart?.remove());
</script>

<div class="relative h-[200px]" bind:this={container} role="img" aria-label={label}>
	{#if !history || (history.curve ?? []).length < 2}
		<div class="absolute inset-0 grid place-items-center px-4 text-center text-[12px] text-sc-ink3">{emptyText}</div>
	{/if}
</div>
