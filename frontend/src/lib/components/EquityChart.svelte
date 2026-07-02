<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type { IChartApi, ISeriesApi, SeriesMarker, UTCTimestamp } from 'lightweight-charts';
	import type { EquityPoint, OHLCVBar } from '$lib/api';

	export let data: EquityPoint[] = [];
	export let height: number = 300;
	export let showDrawdown: boolean = true;
	export let benchmarkData: EquityPoint[] | null = null;
	/** Legend/series title for the benchmark line (e.g. a compared run's id). */
	export let benchmarkTitle: string = 'Buy & Hold';
	export let candleData: OHLCVBar[] | null = null;
	/** When set, shade the in-sample portion (time < this) and draw a divider at the
	 *  out-of-sample start. Used to show the full IS+OOS backtest in one chart. */
	export let oosStartTimestamp: string | null = null;
	/** Event annotations drawn on the equity line (snapped to the nearest data point). */
	export let annotations: Array<{ timestamp: string; label: string; color?: string }> = [];

	let chartContainer: HTMLDivElement;
	let chart: IChartApi | null = null;

	// Track all series for teardown
	let candleSeries: ISeriesApi<'Candlestick'> | null = null;
	let volumeSeries: ISeriesApi<'Histogram'> | null = null;
	let drawdownSeries: ISeriesApi<'Area'> | null = null;
	let benchmarkSeries: ISeriesApi<'Line'> | null = null;
	let equitySeries: ISeriesApi<'Line'> | null = null;
	let isAreaSeries: ISeriesApi<'Area'> | null = null;
	let resizeObserver: ResizeObserver | null = null;

	$: if (chart && data.length > 0) {
		rebuildSeries(data, benchmarkData, candleData, oosStartTimestamp, annotations, benchmarkTitle);
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
				visible: true,
			},
			leftPriceScale: {
				borderColor: '#222222',
				visible: false,
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

		if (data.length > 0) {
			rebuildSeries(data, benchmarkData, candleData, oosStartTimestamp, annotations, benchmarkTitle);
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

	/** Remove all existing series so we can rebuild in correct z-order */
	function teardownSeries() {
		if (!chart) return;
		if (candleSeries) { chart.removeSeries(candleSeries); candleSeries = null; }
		if (volumeSeries) { chart.removeSeries(volumeSeries); volumeSeries = null; }
		if (drawdownSeries) { chart.removeSeries(drawdownSeries); drawdownSeries = null; }
		if (benchmarkSeries) { chart.removeSeries(benchmarkSeries); benchmarkSeries = null; }
		if (isAreaSeries) { chart.removeSeries(isAreaSeries); isAreaSeries = null; }
		if (equitySeries) { chart.removeSeries(equitySeries); equitySeries = null; }
	}

	/**
	 * Rebuild all series from scratch in the correct z-order:
	 *   1. Candles (back)
	 *   2. Volume (back)
	 *   3. Drawdown (separate scale, bottom)
	 *   4. Buy & Hold line
	 *   5. Equity line (FRONT — most important)
	 */
	function rebuildSeries(
		equityData: EquityPoint[],
		benchmark: EquityPoint[] | null,
		candles: OHLCVBar[] | null,
		oosStart: string | null = null,
		annotationList: Array<{ timestamp: string; label: string; color?: string }> = [],
		benchmarkSeriesTitle: string = 'Buy & Hold',
	) {
		if (!chart) return;

		// Parse equity data
		let chartData = equityData
			.map((point) => {
				const ts = parseTimestamp(point.timestamp);
				if (!ts) return null;
				return { time: ts as UTCTimestamp, value: point.equity };
			})
			.filter((d): d is { time: UTCTimestamp; value: number } => d !== null)
			.sort((a, b) => (a.time as number) - (b.time as number));

		// Bound the rendered point count (equity, plus the IS/OOS split and drawdown
		// are all derived from this) so large full-window curves can't freeze the page.
		chartData = capPoints(chartData);

		if (chartData.length === 0) return;

		const equityStartTime = chartData[0].time as number;
		const equityEndTime = chartData[chartData.length - 1].time as number;
		const initialEquity = chartData[0].value;

		// Tear down everything so we control z-order
		teardownSeries();

		// --- 1. Candles (bottom layer) ---
		const hasCandles = candles && candles.length > 0;
		let filteredCandles: Array<OHLCVBar & { _ts: number }> = [];

		if (hasCandles) {
			chart.applyOptions({ leftPriceScale: { visible: true } });

			filteredCandles = candles!
				.map((bar) => {
					const ts = parseTimestamp(bar.timestamp);
					if (!ts) return null;
					return { ...bar, _ts: ts };
				})
				.filter((b): b is OHLCVBar & { _ts: number } => b !== null && b._ts >= equityStartTime && b._ts <= equityEndTime)
				.sort((a, b) => a._ts - b._ts);

			if (filteredCandles.length > 0) {
				candleSeries = chart.addCandlestickSeries({
					upColor: 'rgba(34, 197, 94, 0.35)',
					downColor: 'rgba(239, 68, 68, 0.3)',
					borderUpColor: 'rgba(34, 197, 94, 0.45)',
					borderDownColor: 'rgba(239, 68, 68, 0.4)',
					wickUpColor: 'rgba(34, 197, 94, 0.3)',
					wickDownColor: 'rgba(239, 68, 68, 0.25)',
					priceScaleId: 'left',
					priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
				});

				candleSeries.setData(filteredCandles.map((b) => ({
					time: b._ts as UTCTimestamp,
					open: b.open,
					high: b.high,
					low: b.low,
					close: b.close,
				})));

				chart.priceScale('left').applyOptions({
					scaleMargins: { top: 0.05, bottom: 0.1 },
				});
			}
		} else {
			chart.applyOptions({ leftPriceScale: { visible: false } });
		}

		// --- 2. Volume (bottom layer) ---
		if (filteredCandles.length > 0) {
			volumeSeries = chart.addHistogramSeries({
				priceScaleId: 'volume',
				priceFormat: { type: 'volume' },
			});
			chart.priceScale('volume').applyOptions({
				scaleMargins: { top: 0.92, bottom: 0 },
			});
			volumeSeries.setData(filteredCandles.map((b) => ({
				time: b._ts as UTCTimestamp,
				value: b.volume,
				color: b.close >= b.open ? 'rgba(34, 197, 94, 0.12)' : 'rgba(239, 68, 68, 0.12)',
			})));
		}

		// --- 3. Drawdown (separate scale at bottom) ---
		if (showDrawdown && chartData.length > 1) {
			let peak = chartData[0].value;
			const drawdownData = chartData.map((point) => {
				if (point.value > peak) peak = point.value;
				const dd = ((point.value - peak) / peak) * 100;
				return { time: point.time, value: dd };
			});

			if (drawdownData.some((d) => d.value < -0.01)) {
				drawdownSeries = chart.addAreaSeries({
					priceScaleId: 'drawdown',
					lineColor: 'rgba(239, 68, 68, 0.6)',
					topColor: 'rgba(239, 68, 68, 0.0)',
					bottomColor: 'rgba(239, 68, 68, 0.3)',
					lineWidth: 1,
					priceFormat: {
						type: 'custom',
						formatter: (price: number) => price.toFixed(1) + '%',
					},
					crosshairMarkerVisible: false,
				});
				chart.priceScale('drawdown').applyOptions({
					scaleMargins: { top: 0.82, bottom: 0 },
				});
				drawdownSeries.setData(drawdownData);
			}
		}

		// --- 4. Buy & Hold line ---
		// Use explicit benchmark if provided, otherwise compute from candle data
		const benchmarkChartData: Array<{ time: UTCTimestamp; value: number }> = [];

		if (benchmark && benchmark.length > 0) {
			for (const point of benchmark) {
				const ts = parseTimestamp(point.timestamp);
				if (ts) benchmarkChartData.push({ time: ts as UTCTimestamp, value: point.equity });
			}
			benchmarkChartData.sort((a, b) => (a.time as number) - (b.time as number));
		} else if (filteredCandles.length > 1) {
			const firstClose = filteredCandles[0].close;
			if (firstClose > 0) {
				for (const b of filteredCandles) {
					benchmarkChartData.push({
						time: b._ts as UTCTimestamp,
						value: initialEquity * (b.close / firstClose),
					});
				}
			}
		}

		if (benchmarkChartData.length > 0) {
			benchmarkSeries = chart.addLineSeries({
				color: '#f59e0b',
				lineWidth: 1,
				lineStyle: 2,
				priceScaleId: 'right',
				title: benchmarkSeriesTitle,
				priceFormat: {
					type: 'custom',
					formatter: (price: number) => '$' + price.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 }),
				},
			});
			benchmarkSeries.setData(capPoints(benchmarkChartData));
		}

		// --- 5. Equity line (FRONT — on top of everything) ---
		// With an OOS divider, shade the in-sample portion (area, dimmer) and draw the
		// out-of-sample portion as the bright line with a marker at the OOS start — so a
		// full IS+OOS curve reads honestly in one view. Otherwise draw one bright line.
		const usdFormat = {
			type: 'custom' as const,
			formatter: (price: number) => '$' + price.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 }),
		};
		const oosStartSec = oosStart ? parseTimestamp(oosStart) : null;
		const hasSplit =
			oosStartSec !== null &&
			chartData.some((p) => (p.time as number) < (oosStartSec as number)) &&
			chartData.some((p) => (p.time as number) >= (oosStartSec as number));

		if (hasSplit) {
			// Boundary point is included in BOTH series so IS and OOS connect seamlessly.
			const isData = chartData.filter((p) => (p.time as number) <= (oosStartSec as number));
			const oosData = chartData.filter((p) => (p.time as number) >= (oosStartSec as number));

			isAreaSeries = chart.addAreaSeries({
				priceScaleId: 'right',
				lineColor: 'rgba(34, 211, 238, 0.45)',
				topColor: 'rgba(34, 211, 238, 0.12)',
				bottomColor: 'rgba(34, 211, 238, 0.0)',
				lineWidth: 2,
				title: 'In-sample',
				priceFormat: usdFormat,
				crosshairMarkerVisible: false,
			});
			isAreaSeries.setData(isData.map((p) => ({ time: p.time, value: p.value })));

			equitySeries = chart.addLineSeries({
				color: '#22d3ee',
				lineWidth: 3,
				priceScaleId: 'right',
				title: 'Out-of-sample',
				priceFormat: usdFormat,
			});
			equitySeries.setData(oosData);
		} else {
			equitySeries = chart.addLineSeries({
				color: '#22d3ee',
				lineWidth: 3,
				priceScaleId: 'right',
				title: 'Strategy',
				priceFormat: usdFormat,
			});
			equitySeries.setData(chartData);
		}

		// --- Markers: OOS divider + event annotations (both on the front series) ---
		const seriesMarkers: SeriesMarker<UTCTimestamp>[] = [];
		const frontData = hasSplit
			? chartData.filter((p) => (p.time as number) >= (oosStartSec as number))
			: chartData;
		if (hasSplit && frontData[0]) {
			seriesMarkers.push({
				time: frontData[0].time,
				position: 'aboveBar',
				color: '#67e8f9',
				shape: 'arrowDown',
				text: 'OOS',
			});
		}
		if (annotationList.length > 0 && frontData.length > 0) {
			// Snap each annotation to the nearest rendered point so the marker always
			// lands on the series (lightweight-charts anchors markers to data times).
			const times = frontData.map((p) => p.time as number);
			for (const note of annotationList.slice(0, 30)) {
				const ts = parseTimestamp(note.timestamp);
				if (ts === null) continue;
				let nearest = times[0];
				for (const t of times) {
					if (Math.abs(t - ts) < Math.abs(nearest - ts)) nearest = t;
				}
				seriesMarkers.push({
					time: nearest as UTCTimestamp,
					position: 'belowBar',
					color: note.color ?? '#a78bfa',
					shape: 'circle',
					text: note.label,
				});
			}
		}
		if (seriesMarkers.length > 0 && equitySeries) {
			seriesMarkers.sort((a, b) => (a.time as number) - (b.time as number));
			equitySeries.setMarkers(seriesMarkers);
		}

		// Set right scale margins
		chart.priceScale('right').applyOptions({
			scaleMargins: { top: 0.05, bottom: showDrawdown ? 0.2 : 0.05 },
		});

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

	// Hard cap on points rendered per series. The chart is at most ~2000px wide, so
	// extra points are invisible AND can lock up the canvas on heavier datasets
	// (notably the full-window equity/benchmark curves) — worse on some GPUs.
	// Stride-sample evenly, always preserving the first and last point so the span
	// and final value stay exact.
	function capPoints<T>(points: T[], max = 1500): T[] {
		if (points.length <= max) return points;
		const stride = points.length / max;
		const out: T[] = [];
		for (let i = 0; i < max; i++) {
			out.push(points[Math.min(points.length - 1, Math.floor(i * stride))]);
		}
		out[out.length - 1] = points[points.length - 1];
		return out;
	}
</script>

<div class="equity-chart" style="height: {height}px" bind:this={chartContainer}></div>

<style>
	.equity-chart {
		width: 100%;
		overflow: hidden;
	}
</style>
