<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';

	interface Trial {
		number: number;
		value: number | null;
		params: Record<string, unknown>;
		state?: string;
	}

	export let trials: Trial[] = [];
	export let height: number = 200;
	export let objective: string = 'sharpe_ratio';

	let chartContainer: HTMLDivElement;
	let chart: IChartApi | null = null;
	let trialSeries: ISeriesApi<'Histogram'> | null = null;
	let bestSeries: ISeriesApi<'Line'> | null = null;
	let resizeObserver: ResizeObserver | null = null;

	$: if (chart && trials.length > 0) {
		updateChart();
	}

	onMount(async () => {
		if (!chartContainer) return;
		const { createChart, ColorType } = await import('lightweight-charts');

		chart = createChart(chartContainer, {
			layout: {
				background: { type: ColorType.Solid, color: '#050505' },
				textColor: '#888888',
			},
			grid: {
				vertLines: { color: '#1a1a1a' },
				horzLines: { color: '#1a1a1a' },
			},
			width: chartContainer.clientWidth,
			height: height,
			rightPriceScale: {
				borderColor: '#222222',
			},
			timeScale: {
				borderColor: '#222222',
				visible: true,
				tickMarkFormatter: (time: number) => `#${time}`,
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

		if (trials.length > 0) {
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
		if (!chart || trials.length === 0) return;

		// Clear existing series
		if (trialSeries) { chart.removeSeries(trialSeries); trialSeries = null; }
		if (bestSeries) { chart.removeSeries(bestSeries); bestSeries = null; }

		// Sort trials by number to get chronological order
		const sortedTrials = [...trials]
			.filter(t => t.value !== null && t.state === 'TrialState.COMPLETE')
			.sort((a, b) => a.number - b.number);

		if (sortedTrials.length === 0) return;

		// Find best value for color coding
		const maxValue = Math.max(...sortedTrials.map(t => t.value || 0));
		const minValue = Math.min(...sortedTrials.map(t => t.value || 0));

		// Create histogram for trial values
		trialSeries = chart.addHistogramSeries({
			priceFormat: {
				type: 'price',
				precision: 4,
				minMove: 0.0001,
			},
		});

		const histogramData = sortedTrials.map(t => {
			const value = t.value || 0;
			const normalized = maxValue !== minValue ? (value - minValue) / (maxValue - minValue) : 0.5;
			
			// Color gradient from red (bad) to green (good)
			const r = Math.round(239 * (1 - normalized) + 34 * normalized);
			const g = Math.round(68 * (1 - normalized) + 197 * normalized);
			const b = Math.round(68 * (1 - normalized) + 94 * normalized);
			
			return {
				time: t.number as UTCTimestamp,
				value: value,
				color: `rgba(${r}, ${g}, ${b}, 0.7)`,
			};
		});

		trialSeries.setData(histogramData);

		// Add running best line
		bestSeries = chart.addLineSeries({
			color: '#f59e0b',
			lineWidth: 2,
			priceLineVisible: false,
			lastValueVisible: true,
		});

		let runningBest = -Infinity;
		const bestData = sortedTrials.map(t => {
			if ((t.value || 0) > runningBest) {
				runningBest = t.value || 0;
			}
			return {
				time: t.number as UTCTimestamp,
				value: runningBest,
			};
		});

		bestSeries.setData(bestData);

		chart.timeScale().fitContent();
	}
</script>

<div class="optimization-chart">
	<div class="chart-header">
		<span class="text-[10px] text-[#666] uppercase tracking-wider">Trial Performance ({objective})</span>
	</div>
	<div class="chart-container" style="height: {height}px" bind:this={chartContainer}></div>
	<div class="legend">
		<span class="legend-item"><span class="bar"></span> Trial Value</span>
		<span class="legend-item"><span class="line amber"></span> Best So Far</span>
	</div>
</div>

<style>
	.optimization-chart {
		width: 100%;
	}
	.chart-header {
		padding: 0.25rem 0;
	}
	.chart-container {
		width: 100%;
		overflow: hidden;
	}
	.legend {
		display: flex;
		justify-content: center;
		gap: 1.5rem;
		margin-top: 0.5rem;
		font-size: 10px;
		color: #888888;
	}
	.legend-item {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	.bar {
		display: inline-block;
		width: 12px;
		height: 12px;
		background: linear-gradient(to right, rgba(239, 68, 68, 0.7), rgba(34, 197, 94, 0.7));
	}
	.line {
		display: inline-block;
		width: 20px;
		height: 2px;
		border-top: 2px solid;
	}
	.line.amber { border-color: #f59e0b; }
</style>
