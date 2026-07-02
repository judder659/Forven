<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';
	import type { LabRegimeTimelinePricePoint, LabRegimeTimelineSegment } from '$lib/api';
	import { formatRegimeLabel, regimeSwatchClass } from '$lib/utils/labRegime';

	export let segments: LabRegimeTimelineSegment[] = [];
	export let pricePoints: LabRegimeTimelinePricePoint[] = [];

	let chartContainer: HTMLDivElement;
	let chart: IChartApi | null = null;
	let lineSeries: ISeriesApi<'Line'> | null = null;
	let resizeObserver: ResizeObserver | null = null;

	function segmentColor(regime: string): string {
		return regimeSwatchClass(regime) || 'bg-[#555]';
	}

	function parseTimestamp(timestamp: string): UTCTimestamp | null {
		const value = Date.parse(timestamp);
		if (!Number.isFinite(value)) return null;
		return Math.floor(value / 1000) as UTCTimestamp;
	}

	function updateChart() {
		if (!chart || !lineSeries) return;
		const chartData = pricePoints
			.map((point) => {
				const time = parseTimestamp(point.ts);
				const value = Number(point.close);
				if (time == null || !Number.isFinite(value)) return null;
				return { time, value };
			})
			.filter((point): point is { time: UTCTimestamp; value: number } => point !== null)
			.sort((left, right) => Number(left.time) - Number(right.time));

		lineSeries.setData(chartData);
		if (chartData.length > 1) {
			chart.timeScale().fitContent();
		}
	}

	async function initializeChart() {
		if (!chartContainer || chart) return;
		const { createChart, ColorType, CrosshairMode } = await import('lightweight-charts');
		chart = createChart(chartContainer, {
			layout: {
				background: { type: ColorType.Solid, color: 'transparent' },
				textColor: '#888888',
				fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
			},
			grid: {
				vertLines: { visible: false },
				horzLines: { color: 'rgba(34, 34, 34, 0.9)' },
			},
			width: chartContainer.clientWidth,
			height: chartContainer.clientHeight,
			rightPriceScale: {
				visible: false,
				borderVisible: false,
			},
			leftPriceScale: {
				visible: false,
				borderVisible: false,
			},
			timeScale: {
				visible: false,
				borderVisible: false,
			},
			crosshair: {
				mode: CrosshairMode.Normal,
				horzLine: { visible: false, labelVisible: false },
				vertLine: { color: 'rgba(148, 163, 184, 0.28)', width: 1, labelVisible: false },
			},
			handleScroll: false,
			handleScale: false,
		});

		lineSeries = chart.addLineSeries({
			color: '#f8fafc',
			lineWidth: 2,
			priceLineVisible: false,
			lastValueVisible: false,
			crosshairMarkerVisible: true,
			crosshairMarkerRadius: 3,
			crosshairMarkerBorderColor: '#f8fafc',
			crosshairMarkerBackgroundColor: '#0f172a',
		});
		updateChart();

		resizeObserver = new ResizeObserver(() => {
			if (!chart || !chartContainer) return;
			chart.applyOptions({
				width: chartContainer.clientWidth,
				height: chartContainer.clientHeight,
			});
			updateChart();
		});
		resizeObserver.observe(chartContainer);
	}

	$: totalBars = segments.reduce((sum, segment) => sum + Math.max(0, Number(segment.bars_count) || 0), 0);
	$: if (chart && lineSeries) {
		updateChart();
	}

	onMount(() => {
		void initializeChart();
	});

	onDestroy(() => {
		resizeObserver?.disconnect();
		chart?.remove();
		chart = null;
		lineSeries = null;
	});
</script>

<div class="mt-3 border border-[#222] bg-[#050505] p-2">
	<div class="relative h-28 overflow-hidden border border-[#1a1a1a] bg-black">
		<div class="pointer-events-none absolute inset-0 flex overflow-hidden">
			{#each segments as segment}
				<div
					class={`h-full ${segmentColor(segment.display_regime || segment.regime)} ${Number(segment.uncertain_share ?? 0) > 0 ? 'opacity-28' : 'opacity-20'}`}
					style={`width: ${totalBars > 0 ? (Number(segment.bars_count) / totalBars) * 100 : 0}%`}
					title={`${formatRegimeLabel(segment)} | ${segment.bars_count} bars | confidence ${(segment.confidence_avg * 100).toFixed(1)}% | uncertain ${((segment.uncertain_share ?? 0) * 100).toFixed(1)}%`}
				></div>
			{/each}
		</div>
		<div class="pointer-events-none absolute inset-x-0 top-1/2 border-t border-dashed border-[#333]"></div>
		<div class="absolute inset-x-0 top-0 bottom-4" bind:this={chartContainer}></div>
		<div class="pointer-events-none absolute inset-x-0 bottom-0 flex h-4 overflow-hidden border-t border-[#1a1a1a]">
			{#each segments as segment}
				<div
					class={`h-full ${segmentColor(segment.display_regime || segment.regime)} ${Number(segment.uncertain_share ?? 0) > 0 ? 'opacity-95' : 'opacity-90'}`}
					style={`width: ${totalBars > 0 ? (Number(segment.bars_count) / totalBars) * 100 : 0}%`}
				></div>
			{/each}
		</div>
		{#if pricePoints.length === 0}
			<div class="absolute inset-0 flex items-center justify-center bg-black/55 text-xs text-[#666]">
				Price overlay unavailable for this snapshot.
			</div>
		{/if}
	</div>
	<div class="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-[#666]">
		<div class="flex items-center gap-2">
			<span class="h-[2px] w-6 bg-white"></span>
			<span>Close</span>
		</div>
		<div class="flex items-center gap-2">
			<span class="h-2 w-2 bg-[#888]"></span>
			<span>Classifier segment background</span>
		</div>
	</div>
</div>
