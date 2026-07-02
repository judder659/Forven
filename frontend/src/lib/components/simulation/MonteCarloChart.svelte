<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';

	export let equityPaths: number[][] = []; // Array of equity paths
	export let height: number = 250;

	let chartContainer: HTMLDivElement;
	let chart: IChartApi | null = null;
	let pathSeries: ISeriesApi<'Line'>[] = [];
	let medianSeries: ISeriesApi<'Line'> | null = null;
	let band5Series: ISeriesApi<'Line'> | null = null;
	let band95Series: ISeriesApi<'Line'> | null = null;
	let resizeObserver: ResizeObserver | null = null;

	$: if (chart && equityPaths.length > 0) {
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
				visible: false, // Hide time axis for simulated paths
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

		if (equityPaths.length > 0) {
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
		if (!chart || equityPaths.length === 0) return;

		// Clear existing series
		pathSeries.forEach(s => chart?.removeSeries(s));
		pathSeries = [];
		if (medianSeries) { chart.removeSeries(medianSeries); medianSeries = null; }
		if (band5Series) { chart.removeSeries(band5Series); band5Series = null; }
		if (band95Series) { chart.removeSeries(band95Series); band95Series = null; }

		const numPaths = Math.min(equityPaths.length, 50); // Limit to 50 paths for performance
		const pathLength = equityPaths[0]?.length || 0;

		if (pathLength === 0) return;

		// Calculate percentile bands at each time step
		const p5: number[] = [];
		const p50: number[] = [];
		const p95: number[] = [];

		for (let t = 0; t < pathLength; t++) {
			const valuesAtT = equityPaths.map(path => path[t]).sort((a, b) => a - b);
			p5.push(valuesAtT[Math.floor(valuesAtT.length * 0.05)] || valuesAtT[0]);
			p50.push(valuesAtT[Math.floor(valuesAtT.length * 0.5)] || valuesAtT[0]);
			p95.push(valuesAtT[Math.floor(valuesAtT.length * 0.95)] || valuesAtT[valuesAtT.length - 1]);
		}

		// Add individual paths (semi-transparent)
		for (let i = 0; i < numPaths; i++) {
			const path = equityPaths[i];
			const finalReturn = (path[path.length - 1] - path[0]) / path[0];
			
			// Color based on final return
			const color = finalReturn > 0 
				? 'rgba(34, 197, 94, 0.15)' // green
				: 'rgba(239, 68, 68, 0.15)'; // red

			const series = chart.addLineSeries({
				color: color,
				lineWidth: 1,
				priceLineVisible: false,
				lastValueVisible: false,
				crosshairMarkerVisible: false,
			});

			const data = path.map((value, idx) => ({
				time: idx as UTCTimestamp,
				value: value,
			}));

			series.setData(data);
			pathSeries.push(series);
		}

		// Add 5th percentile band (lower bound)
		band5Series = chart.addLineSeries({
			color: 'rgba(239, 68, 68, 0.6)',
			lineWidth: 2,
			lineStyle: 2, // Dashed
			priceLineVisible: false,
			lastValueVisible: false,
		});
		band5Series.setData(p5.map((v, i) => ({ time: i as UTCTimestamp, value: v })));

		// Add median line
		medianSeries = chart.addLineSeries({
			color: '#f59e0b',
			lineWidth: 2,
			priceLineVisible: false,
			lastValueVisible: true,
		});
		medianSeries.setData(p50.map((v, i) => ({ time: i as UTCTimestamp, value: v })));

		// Add 95th percentile band (upper bound)
		band95Series = chart.addLineSeries({
			color: 'rgba(34, 197, 94, 0.6)',
			lineWidth: 2,
			lineStyle: 2, // Dashed
			priceLineVisible: false,
			lastValueVisible: false,
		});
		band95Series.setData(p95.map((v, i) => ({ time: i as UTCTimestamp, value: v })));

		chart.timeScale().fitContent();
	}
</script>

<div class="monte-carlo-chart">
	<div class="chart-container" style="height: {height}px" bind:this={chartContainer}></div>
	<div class="legend">
		<span class="legend-item"><span class="line dashed green"></span> 95th %ile</span>
		<span class="legend-item"><span class="line solid amber"></span> Median</span>
		<span class="legend-item"><span class="line dashed red"></span> 5th %ile</span>
	</div>
</div>

<style>
	.monte-carlo-chart {
		width: 100%;
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
	.line {
		display: inline-block;
		width: 20px;
		height: 2px;
	}
	.line.solid { border-top: 2px solid; }
	.line.dashed { border-top: 2px dashed; }
	.line.green { border-color: rgba(34, 197, 94, 0.8); }
	.line.amber { border-color: #f59e0b; }
	.line.red { border-color: rgba(239, 68, 68, 0.8); }
</style>
