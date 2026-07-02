<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';
	import type { OHLCVBar } from '$lib/api';

	export let data: OHLCVBar[] = [];
	export let height: number = 400;
	export let showVolume: boolean = true;

	// Indicator overlay support
	type IndicatorOverlay = {
		name: string;
		color: string;
		data: Array<{ timestamp: string; value: number }>;
		panel?: 'main' | 'sub1' | 'sub2';
	};
	export let indicators: IndicatorOverlay[] = [];

	// Signal marker support
	interface SignalMarker {
		timestamp: string;
		price: number;
		type: 'entry' | 'exit';
	}
	export let signals: { entries: SignalMarker[]; exits: SignalMarker[] } | null = null;

	let chartContainer: HTMLDivElement;
	let chart: IChartApi | null = null;
	let candleSeries: ISeriesApi<'Candlestick'> | null = null;
	let volumeSeries: ISeriesApi<'Histogram'> | null = null;
	let indicatorSeries: Map<string, ISeriesApi<'Line'>> = new Map();
	let resizeObserver: ResizeObserver | null = null;

	$: if (chart && data.length > 0) {
		updateChart(data);
	}

	$: if (chart && indicators) {
		updateIndicators(indicators);
	}

	$: if (chart && candleSeries && signals) {
		updateMarkers(signals.entries, signals.exits);
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
				secondsVisible: false,
			},
			crosshair: {
				mode: 1,
			},
		});

		// Create candlestick series
		candleSeries = chart.addCandlestickSeries({
			upColor: '#22c55e',
			downColor: '#ef4444',
			borderUpColor: '#22c55e',
			borderDownColor: '#ef4444',
			wickUpColor: '#22c55e',
			wickDownColor: '#ef4444',
		});

		// Create volume series if enabled
		if (showVolume) {
			volumeSeries = chart.addHistogramSeries({
				priceScaleId: 'volume',
				priceFormat: {
					type: 'volume',
				},
			});

			chart.priceScale('volume').applyOptions({
				scaleMargins: {
					top: 0.85,
					bottom: 0,
				},
			});
		}

		// Handle resize
		resizeObserver = new ResizeObserver(() => {
			if (chart && chartContainer) {
				chart.applyOptions({ width: chartContainer.clientWidth });
			}
		});
		resizeObserver.observe(chartContainer);

		if (data.length > 0) {
			updateChart(data);
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

	function updateChart(ohlcvData: OHLCVBar[]) {
		if (!candleSeries || !chart) return;

		// Parse and format OHLCV data
		const chartData = ohlcvData
			.map((bar) => {
				const timestamp = parseTimestamp(bar.timestamp);
				if (!timestamp) return null;
				return {
					time: timestamp as UTCTimestamp,
					open: bar.open,
					high: bar.high,
					low: bar.low,
					close: bar.close,
				};
			})
			.filter((d): d is { time: UTCTimestamp; open: number; high: number; low: number; close: number } => d !== null)
			.sort((a, b) => (a.time as number) - (b.time as number));

		if (chartData.length === 0) return;

		candleSeries.setData(chartData);

		// Update volume series if enabled
		if (showVolume && volumeSeries) {
			const volumeData = ohlcvData
				.map((bar) => {
					const timestamp = parseTimestamp(bar.timestamp);
					if (!timestamp) return null;
					const isUp = bar.close >= bar.open;
					return {
						time: timestamp as UTCTimestamp,
						value: bar.volume,
						color: isUp ? 'rgba(34, 197, 94, 0.5)' : 'rgba(239, 68, 68, 0.5)',
					};
				})
				.filter((d): d is { time: UTCTimestamp; value: number; color: string } => d !== null)
				.sort((a, b) => (a.time as number) - (b.time as number));

			volumeSeries.setData(volumeData);
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

	function updateIndicators(overlays: IndicatorOverlay[]) {
		if (!chart) return;

		// Get current indicator names
		const currentNames = new Set(indicatorSeries.keys());
		const newNames = new Set(overlays.filter(o => o.panel === 'main' || !o.panel).map(o => o.name));

		// Remove indicators that are no longer in the list
		for (const name of currentNames) {
			if (!newNames.has(name)) {
				const series = indicatorSeries.get(name);
				if (series) {
					chart.removeSeries(series);
					indicatorSeries.delete(name);
				}
			}
		}

		// Add or update indicators (only 'main' panel overlays)
		for (const overlay of overlays) {
			if (overlay.panel && overlay.panel !== 'main') continue;
			if (!overlay.data || overlay.data.length === 0) continue;

			let series = indicatorSeries.get(overlay.name);

			// Create series if it doesn't exist
			if (!series) {
				series = chart.addLineSeries({
					color: overlay.color || '#3b82f6',
					lineWidth: 2,
					title: overlay.name,
					priceLineVisible: false,
					lastValueVisible: true,
				});
				indicatorSeries.set(overlay.name, series);
			} else {
				// Update color if changed
				series.applyOptions({ color: overlay.color || '#3b82f6' });
			}

			// Format and set data
			const lineData = overlay.data
				.map((point) => {
					const timestamp = parseTimestamp(point.timestamp);
					if (!timestamp || point.value === undefined || point.value === null || isNaN(point.value)) return null;
					return {
						time: timestamp as UTCTimestamp,
						value: point.value,
					};
				})
				.filter((d): d is { time: UTCTimestamp; value: number } => d !== null)
				.sort((a, b) => (a.time as number) - (b.time as number));

			if (lineData.length > 0) {
				series.setData(lineData);
			}
		}
	}

	function snapToBar(ts: number): UTCTimestamp | null {
		if (!data || data.length === 0) return null;
		let best = parseTimestamp(data[0].timestamp);
		if (best === null) return null;
		let bestDist = Math.abs(ts - best);
		for (const bar of data) {
			const bt = parseTimestamp(bar.timestamp);
			if (bt === null) continue;
			const d = Math.abs(ts - bt);
			if (d < bestDist) {
				best = bt;
				bestDist = d;
			}
		}
		return best as UTCTimestamp;
	}

	function updateMarkers(entries: SignalMarker[], exits: SignalMarker[]) {
		if (!candleSeries) return;

		const markers = [
			...entries.map((m) => {
				const raw = parseTimestamp(m.timestamp);
				if (raw === null) return null;
				const t = snapToBar(raw);
				if (t === null) return null;
				return {
					time: t,
					position: 'belowBar' as const,
					color: '#22c55e',
					shape: 'arrowUp' as const,
					text: 'BUY',
				};
			}),
			...exits.map((m) => {
				const raw = parseTimestamp(m.timestamp);
				if (raw === null) return null;
				const t = snapToBar(raw);
				if (t === null) return null;
				return {
					time: t,
					position: 'aboveBar' as const,
					color: '#ef4444',
					shape: 'arrowDown' as const,
					text: 'SELL',
				};
			}),
		]
			.filter((m): m is NonNullable<typeof m> => m !== null)
			.sort((a, b) => (a.time as number) - (b.time as number));

		candleSeries.setMarkers(markers);
	}

	// Method to clear all markers (can be called from parent)
	export function clearMarkers() {
		if (!candleSeries) return;
		candleSeries.setMarkers([]);
	}

	// Method to clear all indicators (can be called from parent)
	export function clearIndicators() {
		if (!chart) return;
		for (const [name, series] of indicatorSeries) {
			chart.removeSeries(series);
		}
		indicatorSeries.clear();
	}
</script>

<div class="candlestick-chart" bind:this={chartContainer}></div>

<style>
	.candlestick-chart {
		width: 100%;
		overflow: hidden;
	}
</style>
