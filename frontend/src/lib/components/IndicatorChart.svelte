<script lang="ts">
	import { CHART_THEME } from '$lib/utils/chartTheme';
	import { onMount, onDestroy } from 'svelte';
	import { createChart, type IChartApi, type ISeriesApi, ColorType, type UTCTimestamp } from 'lightweight-charts';

	export let data: { timestamp: string; value: number }[] = [];
	export let height: number = 300;
	export let lineColor: string = '#3b82f6';

	let chartContainer: HTMLDivElement;
	let chart: IChartApi | null = null;
	let lineSeries: ISeriesApi<'Line'> | null = null;

	$: if (chart && data.length > 0) {
		updateChart(data);
	}

	onMount(() => {
		if (!chartContainer) return;

		chart = createChart(chartContainer, {
			layout: {
				background: { type: ColorType.Solid, color: CHART_THEME.background },
				textColor: CHART_THEME.text,
				fontFamily: CHART_THEME.fontFamily,
			},
			grid: {
				vertLines: { color: CHART_THEME.grid },
				horzLines: { color: CHART_THEME.grid },
			},
			width: chartContainer.clientWidth,
			height: height,
			rightPriceScale: {
				borderColor: CHART_THEME.border,
			},
			timeScale: {
				borderColor: CHART_THEME.border,
				timeVisible: true,
			},
			crosshair: {
				mode: 1,
			},
		});

		lineSeries = chart.addLineSeries({
			color: lineColor,
			lineWidth: 2,
		});

		const resizeObserver = new ResizeObserver(() => {
			if (chart && chartContainer) {
				chart.applyOptions({ width: chartContainer.clientWidth });
			}
		});
		resizeObserver.observe(chartContainer);

		if (data.length > 0) {
			updateChart(data);
		}

		return () => {
			resizeObserver.disconnect();
		};
	});

	onDestroy(() => {
		if (chart) {
			chart.remove();
			chart = null;
		}
	});

	function updateChart(indicatorData: { timestamp: string; value: number }[]) {
		if (!lineSeries || !chart) return;

		const chartData = indicatorData
			.map((point) => {
				const timestamp = parseTimestamp(point.timestamp);
				if (!timestamp) return null;
				return {
					time: timestamp as UTCTimestamp,
					value: point.value,
				};
			})
			.filter((d): d is { time: UTCTimestamp; value: number } => d !== null)
			.sort((a, b) => (a.time as number) - (b.time as number));

		if (chartData.length === 0) return;

		lineSeries.setData(chartData);
		chart.timeScale().fitContent();
	}

	function parseTimestamp(timestamp: string): number | null {
		try {
			const date = new Date(timestamp);
			if (!isNaN(date.getTime())) {
				return Math.floor(date.getTime() / 1000);
			}
			return null;
		} catch {
			return null;
		}
	}
</script>

<div class="indicator-chart" bind:this={chartContainer}></div>

<style>
	.indicator-chart {
		width: 100%;
		overflow: hidden;
	}
</style>
