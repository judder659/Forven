<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';
	import type { EquityPoint } from '$lib/api';

	export let series: { name: string; data: EquityPoint[]; color: string }[] = [];
	export let height: number = 400;

	let chartContainer: HTMLDivElement;
	let chart: IChartApi | null = null;
	let lineSeries: Map<string, ISeriesApi<'Line'>> = new Map();
	let resizeObserver: ResizeObserver | null = null;

	$: if (chart && series.length > 0) {
		updateChart();
	}

	onMount(async () => {
		if (!chartContainer) return;
		const { createChart, ColorType } = await import('lightweight-charts');

		chart = createChart(chartContainer, {
			layout: {
				background: { type: ColorType.Solid, color: '#0a0a0a' },
				textColor: '#9ca3af',
			},
			grid: {
				vertLines: { color: '#1c1c1c' },
				horzLines: { color: '#1c1c1c' },
			},
			width: chartContainer.clientWidth,
			height: height,
			rightPriceScale: {
				borderColor: '#222222',
			},
			timeScale: {
				borderColor: '#222222',
				timeVisible: true,
			},
			crosshair: {
				mode: 1,
			},
		});

		resizeObserver = new ResizeObserver(() => {
			if (chart && chartContainer) {
				chart.applyOptions({ width: chartContainer.clientWidth });
			}
		});
		resizeObserver.observe(chartContainer);

		if (series.length > 0) {
			updateChart();
		}
	});

	onDestroy(() => {
		resizeObserver?.disconnect();
		resizeObserver = null;
		if (chart) {
			chart.remove();
			chart = null;
		}
	});

	function updateChart() {
		if (!chart) return;

		// Remove old series that are no longer in the data
		for (const [name, lineSerie] of lineSeries) {
			if (!series.find(s => s.name === name)) {
				chart.removeSeries(lineSerie);
				lineSeries.delete(name);
			}
		}

		// Add or update series
		for (const s of series) {
			let lineSerie = lineSeries.get(s.name);

			if (!lineSerie) {
				lineSerie = chart.addLineSeries({
					color: s.color,
					lineWidth: 2,
					title: s.name,
					priceFormat: {
						type: 'custom',
						formatter: (price: number) => '$' + price.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 }),
					},
				});
				lineSeries.set(s.name, lineSerie);
			}

			const chartData = s.data
				.map((point) => {
					const timestamp = parseTimestamp(point.timestamp);
					if (!timestamp) return null;
					return {
						time: timestamp as UTCTimestamp,
						value: point.equity,
					};
				})
				.filter((d): d is { time: UTCTimestamp; value: number } => d !== null)
				.sort((a, b) => (a.time as number) - (b.time as number));

			lineSerie.setData(chartData);
		}

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

<div class="multi-equity-chart" bind:this={chartContainer}></div>

<style>
	.multi-equity-chart {
		width: 100%;
		overflow: hidden;
	}
</style>
