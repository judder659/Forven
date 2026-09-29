<script lang="ts">
	/** Live account equity after each closed trade, shaded above/below where it started. */
	import { onDestroy, onMount } from 'svelte';
	import { createChart, type IChartApi, type Time } from 'lightweight-charts';
	import type { ForvenEquityHistory } from '$lib/api';
	import { parseTs } from '$lib/utils/tradingDesk/format';

	export let history: ForvenEquityHistory | null = null;

	let container: HTMLDivElement;
	let chart: IChartApi | null = null;

	function render(): void {
		if (!container) return;
		chart?.remove();
		chart = null;
		const base = history?.base;
		const points: Array<{ time: Time; value: number }> = [];
		for (const point of history?.curve ?? []) {
			const ms = parseTs(point.time);
			if (ms === null || !Number.isFinite(point.value)) continue;
			const time = Math.floor(ms / 1000);
			const last = points[points.length - 1];
			if (last && (last.time as number) >= time) last.value = point.value;
			else points.push({ time: time as Time, value: point.value });
		}
		if (points.length < 2 || typeof base !== 'number') return;
		chart = createChart(container, {
			autoSize: true,
			layout: { background: { color: 'transparent' }, textColor: '#747c88', fontFamily: '"IBM Plex Mono", ui-monospace, monospace', fontSize: 10 },
			grid: { vertLines: { visible: false }, horzLines: { color: '#12151a' } },
			rightPriceScale: { borderColor: '#1c2026' },
			timeScale: { borderColor: '#1c2026' },
			handleScroll: false,
			handleScale: false,
		});
		const series = chart.addBaselineSeries({
			baseValue: { type: 'price', price: base },
			topLineColor: '#139a9f',
			topFillColor1: 'rgba(19,154,159,0.22)',
			topFillColor2: 'rgba(19,154,159,0.02)',
			bottomLineColor: '#e0663f',
			bottomFillColor1: 'rgba(224,102,63,0.02)',
			bottomFillColor2: 'rgba(224,102,63,0.22)',
			lineWidth: 2,
			priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
		});
		series.setData(points);
		series.createPriceLine({ price: base, color: '#4b525c', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'Start' });
		chart.timeScale().fitContent();
	}

	onMount(render);
	$: if (container && history) render();
	onDestroy(() => chart?.remove());
</script>

<div class="relative h-[200px]" bind:this={container} role="img" aria-label="Live account equity after each closed trade">
	{#if !history || (history.curve ?? []).length < 2}
		<div class="absolute inset-0 grid place-items-center text-[12px] text-sc-ink3">No closed live trades yet.</div>
	{/if}
</div>
