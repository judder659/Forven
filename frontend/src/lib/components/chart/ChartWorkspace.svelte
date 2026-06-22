<script lang="ts">
	import { createEventDispatcher, onMount, onDestroy } from 'svelte';
	import {
		createChart,
		CrosshairMode,
		LineStyle,
		type IChartApi,
		type IPriceLine,
		type ISeriesApi,
		type MouseEventHandler,
		type SeriesMarker,
		type Time,
	} from 'lightweight-charts';
	import type { OHLCVBar } from '$lib/api';
	import type { SignalMarker, IndicatorConfig } from '$lib/stores/chartStore';
	import type { ChartDrawing, ChartDrawingPoint, ChartDrawingTool } from './types';

	export let data: OHLCVBar[] = [];
	export let entryMarkers: SignalMarker[] = [];
	export let exitMarkers: SignalMarker[] = [];
	export let mainIndicators: IndicatorConfig[] = [];
	export let subIndicators: IndicatorConfig[] = [];
	export let strategyName: string | null = null;
	export let strategyMeta: string | null = null;
	export let strategyParams: Record<string, unknown> = {};
	export let showStrategyInfo = false;
	export let autoScroll = false;
	export let windowSize = 200;
	export let drawings: ChartDrawing[] = [];
	export let activeTool: ChartDrawingTool = 'cursor';
	export let fitContentToken = 0;

	const dispatch = createEventDispatcher<{
		drawingPoint: ChartDrawingPoint;
	}>();

	let chartContainer: HTMLDivElement;
	let chart: IChartApi | null = null;
	let candleSeries: ISeriesApi<'Candlestick'> | null = null;
	let volumeSeries: ISeriesApi<'Histogram'> | null = null;
	
	let mainSeriesMap = new Map<string, ISeriesApi<'Line'>>();
	let subSeriesMap = new Map<string, ISeriesApi<'Line'>>();
	let drawingSeriesMap = new Map<string, ISeriesApi<'Line'>>();
	let priceLinesMap = new Map<string, IPriceLine>();

	let resizeObserver: ResizeObserver | null = null;
	let latestCandleData: Array<{ time: Time; open: number; high: number; low: number; close: number }> = [];
	let viewportMode: 'initial' | 'manual' = autoScroll ? 'initial' : 'manual';
	let appliedFitContentToken = fitContentToken;
	let suppressVisibleRangeEvents = 0;
	// Becomes true the first time the chart is laid out with a non-zero width. The chart
	// can mount inside a flex child that measures 0px on the first layout pass, and any
	// viewport reset done at that point positions against a zero-width time scale and is
	// effectively lost. We re-anchor on the first real width to recover from that.
	let hasRealWidth = false;

	$: strategyParamEntries = Object.entries(strategyParams || {});
	$: hasSubIndicators = subIndicators && subIndicators.length > 0;

	function formatParamValue(value: unknown): string {
		if (value === null) return 'null';
		if (value === undefined) return '-';
		let rendered: string;
		if (typeof value === 'string') rendered = value;
		else if (typeof value === 'number' || typeof value === 'boolean') rendered = String(value);
		else {
			try {
				rendered = JSON.stringify(value);
			} catch {
				rendered = String(value);
			}
		}
		return rendered.length > 60 ? `${rendered.slice(0, 57)}...` : rendered;
	}

	function parseTimestamp(ts: string): number {
		return new Date(ts).getTime() / 1000;
	}

	function toIsoTimestamp(time: Time | null | undefined): string | null {
		if (time === null || time === undefined) return null;
		if (typeof time === 'number') {
			return new Date(time * 1000).toISOString();
		}
		if (typeof time === 'string') {
			const parsed = new Date(time);
			return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
		}
		if (typeof time === 'object' && 'year' in time) {
			return new Date(Date.UTC(time.year, time.month - 1, time.day)).toISOString();
		}
		return null;
	}

	const handleChartClick: MouseEventHandler<Time> = (param) => {
		if (activeTool === 'cursor' || !chart || !candleSeries || !param.point) return;
		const resolvedTime = param.time ?? chart.timeScale().coordinateToTime(param.point.x);
		const resolvedPrice = candleSeries.coordinateToPrice(param.point.y);
		const isoTime = toIsoTimestamp(resolvedTime);
		if (!isoTime || resolvedPrice === null) return;
		dispatch('drawingPoint', {
			time: isoTime,
			price: Number(resolvedPrice),
		});
	};

	const handleVisibleRangeChange = () => {
		if (!data || data.length === 0) return;
		if (suppressVisibleRangeEvents > 0) return;
		// A range change that isn't one of our own programmatic resets means the user
		// panned/zoomed. Stop following real time so live updates and resizes leave
		// their chosen view alone.
		viewportMode = 'manual';
	};

	function queueVisibleRangeSuppressionRelease(): void {
		// lightweight-charts applies setVisibleRange()/fitContent() and fires the
		// resulting visible-range event on a requestAnimationFrame, NOT synchronously.
		// Releasing the guard on a microtask would drop it before that event fires, so
		// our own reset would be misread as a user pan. Release after the next frame.
		requestAnimationFrame(() => {
			suppressVisibleRangeEvents = Math.max(0, suppressVisibleRangeEvents - 1);
		});
	}

	function resetViewport(forcePriceAutoscale = false): void {
		if (!chart || !candleSeries || latestCandleData.length === 0) return;

		suppressVisibleRangeEvents += 1;
		if (forcePriceAutoscale) {
			candleSeries.priceScale().applyOptions({ autoScale: true });
		}

		if (windowSize > 0 && latestCandleData.length > windowSize) {
			chart.timeScale().setVisibleRange({
				from: latestCandleData[latestCandleData.length - windowSize].time,
				to: latestCandleData[latestCandleData.length - 1].time,
			});
		} else {
			chart.timeScale().fitContent();
		}

		queueVisibleRangeSuppressionRelease();
	}

	onMount(() => {
		if (!chartContainer) return;

		chart = createChart(chartContainer, {
			// Seed with the container's measured size so the first viewport reset has a
			// real width to position against whenever layout is already settled.
			...(chartContainer.clientWidth > 0 ? { width: chartContainer.clientWidth } : {}),
			...(chartContainer.clientHeight > 0 ? { height: chartContainer.clientHeight } : {}),
			layout: {
				background: { color: '#000000' },
				textColor: '#666',
			},
			grid: {
				vertLines: { color: '#111' },
				horzLines: { color: '#111' },
			},
			crosshair: {
				mode: CrosshairMode.Normal,
			},
			rightPriceScale: {
				borderColor: '#222',
				autoScale: true,
			},
			timeScale: {
				borderColor: '#222',
				timeVisible: true,
				secondsVisible: false,
			},
			handleScroll: {
				vertTouchDrag: false,
			}
		});

		// Price Series
		candleSeries = chart.addCandlestickSeries({
			upColor: '#22c55e',
			downColor: '#ef4444',
			borderVisible: false,
			wickUpColor: '#22c55e',
			wickDownColor: '#ef4444',
		});

		// Volume Series
		volumeSeries = chart.addHistogramSeries({
			color: '#26a69a',
			priceFormat: {
				type: 'volume',
			},
			priceScaleId: '', // set as an overlay by using an empty string
		});
		
		volumeSeries.priceScale().applyOptions({
			scaleMargins: {
				top: 0.8,
				bottom: 0,
			},
		});

		updateChartLayout();
		updateData();
		chart.subscribeClick(handleChartClick);
		chart.timeScale().subscribeVisibleTimeRangeChange(handleVisibleRangeChange);

		resizeObserver = new ResizeObserver(entries => {
			if (chart && entries.length > 0) {
				const { width, height } = entries[0].contentRect;
				chart.applyOptions({ width, height });
				if (width > 0) {
					const firstRealWidth = !hasRealWidth;
					hasRealWidth = true;
					// First real width: recover from a zero-width mount by forcing the
					// most-recent frame (the user can't have panned a zero-width chart).
					// Afterwards keep re-framing only while we're still following.
					if (firstRealWidth && autoScroll) {
						viewportMode = 'initial';
						resetViewport(true);
					} else if (viewportMode === 'initial') {
						resetViewport(false);
					}
				}
			}
		});
		resizeObserver.observe(chartContainer);
	});

	onDestroy(() => {
		resizeObserver?.disconnect();
		if (chart) {
			chart.unsubscribeClick(handleChartClick);
			chart.timeScale().unsubscribeVisibleTimeRangeChange(handleVisibleRangeChange);
			chart.remove();
			chart = null;
		}
	});

	// React to changes
	$: if (chart && data) updateData();
	$: if (chart && (entryMarkers || exitMarkers)) updateMarkers();
	$: if (chart && (mainIndicators || subIndicators)) updateIndicators();
	$: if (chart && drawings) updateDrawings();
	$: if (chart && fitContentToken !== appliedFitContentToken) {
		appliedFitContentToken = fitContentToken;
		// "Reset View" / session switch: resume following the most-recent window until
		// the user pans again.
		viewportMode = 'initial';
		resetViewport(true);
	}

	function updateChartLayout() {
		if (!chart || !candleSeries || !volumeSeries) return;

		if (hasSubIndicators) {
			candleSeries.priceScale().applyOptions({
				scaleMargins: { top: 0.05, bottom: 0.35 },
			});
			volumeSeries.priceScale().applyOptions({
				scaleMargins: { top: 0.65, bottom: 0.35 },
			});
		} else {
			candleSeries.priceScale().applyOptions({
				scaleMargins: { top: 0.05, bottom: 0.1 },
			});
			volumeSeries.priceScale().applyOptions({
				scaleMargins: { top: 0.8, bottom: 0 },
			});
		}
	}

	function updateData() {
		if (!candleSeries || !volumeSeries || !data) return;

		const sorted = [...data]
			.map(bar => ({ ...bar, _time: parseTimestamp(bar.timestamp) }))
			.filter(b => !isNaN(b._time))
			.sort((a, b) => a._time - b._time);

		// Deduplicate by time
		const uniqueBars = [];
		let lastTime = 0;
		for (const bar of sorted) {
			if (bar._time !== lastTime) {
				uniqueBars.push(bar);
				lastTime = bar._time;
			} else {
				uniqueBars[uniqueBars.length - 1] = bar;
			}
		}

		const candleData = uniqueBars.map(b => ({
			time: b._time as Time,
			open: b.open,
			high: b.high,
			low: b.low,
			close: b.close
		}));
		latestCandleData = candleData;

		const volumeData = uniqueBars.map(b => ({
			time: b._time as Time,
			value: b.volume,
			color: b.close >= b.open ? 'rgba(34, 197, 94, 0.4)' : 'rgba(239, 68, 68, 0.4)'
		}));

		candleSeries.setData(candleData);
		volumeSeries.setData(volumeData);
		
		updateMarkers();
		updateIndicators();
		updateDrawings();

		// While following (initial mode), re-anchor to the most-recent window on every
		// data update. This keeps the chart pinned to the current timeframe as bars
		// stream in and self-heals an initial reset that landed before real layout —
		// without fighting the user, who switches to 'manual' the moment they pan/zoom.
		// Don't force price autoscale here: it runs on every live poll and would undo a
		// manual price-axis zoom (autoScale stays on by default anyway).
		if (candleData.length > 0 && viewportMode === 'initial') {
			resetViewport(false);
		}
	}

	function snapToBar(ts: number): Time | null {
		if (!data || data.length === 0) return null;
		let best = parseTimestamp(data[0].timestamp);
		let bestDist = Math.abs(ts - best);
		for (const bar of data) {
			const bt = parseTimestamp(bar.timestamp);
			const d = Math.abs(ts - bt);
			if (d < bestDist) {
				best = bt;
				bestDist = d;
			}
		}
		return best as Time;
	}

	function markerDirection(marker: SignalMarker): 'long' | 'short' {
		return String(marker.direction || 'long').toLowerCase() === 'short' ? 'short' : 'long';
	}

	function markerSource(marker: SignalMarker): 'trade' | 'signal' {
		return String(marker.source || 'trade').toLowerCase() === 'signal' ? 'signal' : 'trade';
	}

	function markerText(marker: SignalMarker, fallback: string): string | undefined {
		if (markerSource(marker) === 'signal') return undefined;
		return marker.label || fallback;
	}

	function updateMarkers() {
		if (!candleSeries || !data || data.length === 0) return;

		const markers: SeriesMarker<Time>[] = [];

		for (const m of entryMarkers) {
			const raw = parseTimestamp(m.timestamp);
			if (isNaN(raw)) continue;
			const t = snapToBar(raw);
			if (t === null) continue;
			const direction = markerDirection(m);
			const text = markerText(m, direction === 'short' ? 'Short' : 'Buy');
			markers.push({
				time: t,
				position: direction === 'short' ? 'aboveBar' : 'belowBar',
				color: direction === 'short' ? '#ef4444' : '#22c55e',
				shape: direction === 'short' ? 'arrowDown' : 'arrowUp',
				...(text ? { text } : {}),
			});
		}

		for (const m of exitMarkers) {
			const raw = parseTimestamp(m.timestamp);
			if (isNaN(raw)) continue;
			const t = snapToBar(raw);
			if (t === null) continue;
			const direction = markerDirection(m);
			const text = markerText(m, direction === 'short' ? 'Cover' : 'Sell');
			markers.push({
				time: t,
				position: direction === 'short' ? 'belowBar' : 'aboveBar',
				color: direction === 'short' ? '#22c55e' : '#ef4444',
				shape: direction === 'short' ? 'arrowUp' : 'arrowDown',
				...(text ? { text } : {}),
			});
		}

		// Lightweight charts requires markers to be sorted by time
		markers.sort((a, b) => (a.time as number) - (b.time as number));
		candleSeries.setMarkers(markers);
	}

	function updateIndicators() {
		if (!chart) return;
		
		updateChartLayout();

		// Handle Main Indicators
		const currentMainKeys = new Set(mainIndicators.map(i => i.name));
		for (const [name, series] of mainSeriesMap.entries()) {
			if (!currentMainKeys.has(name)) {
				chart.removeSeries(series);
				mainSeriesMap.delete(name);
			}
		}

		for (const ind of mainIndicators) {
			if (!ind.data || ind.data.length === 0 || ind.visible === false) continue;
			
			let series = mainSeriesMap.get(ind.name);
			if (!series) {
				series = chart.addLineSeries({
					color: ind.color || '#2962FF',
					lineWidth: 2,
					crosshairMarkerVisible: false,
					priceScaleId: 'right', // Same as candles
				});
				mainSeriesMap.set(ind.name, series);
			}

			const lineData = ind.data
				.map(pt => ({ time: parseTimestamp(pt.timestamp) as Time, value: pt.value }))
				.filter(pt => !isNaN(pt.time as number) && pt.value !== null && isFinite(pt.value))
				.sort((a, b) => (a.time as number) - (b.time as number));

			series.setData(lineData);
		}

		// Handle Sub Indicators
		const currentSubKeys = new Set(subIndicators.map(i => i.name));
		for (const [name, series] of subSeriesMap.entries()) {
			if (!currentSubKeys.has(name)) {
				chart.removeSeries(series);
				subSeriesMap.delete(name);
			}
		}

		if (hasSubIndicators) {
			for (const ind of subIndicators) {
				if (!ind.data || ind.data.length === 0 || ind.visible === false) continue;
				
				let series = subSeriesMap.get(ind.name);
				if (!series) {
					series = chart.addLineSeries({
						color: ind.color || '#ff0000',
						lineWidth: 2,
						crosshairMarkerVisible: false,
						priceScaleId: 'sub', // Separate pane
					});
					
					// Configure the separate pane
					series.priceScale().applyOptions({
						scaleMargins: {
							top: 0.75, // Bottom 25% of the chart
							bottom: 0,
						},
					});

					subSeriesMap.set(ind.name, series);
				}

				const lineData = ind.data
					.map(pt => ({ time: parseTimestamp(pt.timestamp) as Time, value: pt.value }))
					.filter(pt => !isNaN(pt.time as number) && pt.value !== null && isFinite(pt.value))
					.sort((a, b) => (a.time as number) - (b.time as number));

				series.setData(lineData);
			}
		}
	}

	function updateDrawings() {
		if (!chart || !candleSeries) return;

		const horizontalLineIds = new Set(
			drawings.filter((drawing) => drawing.type === 'horizontalLine').map((drawing) => drawing.id)
		);

		for (const [id, line] of priceLinesMap.entries()) {
			if (!horizontalLineIds.has(id)) {
				candleSeries.removePriceLine(line);
				priceLinesMap.delete(id);
			}
		}

		for (const drawing of drawings) {
			if (drawing.type !== 'horizontalLine') continue;
			const options = {
				price: drawing.price,
				color: drawing.color || '#f59e0b',
				lineWidth: 2 as const,
				lineStyle: LineStyle.Dashed,
				axisLabelVisible: true,
				title: drawing.label || `@ ${drawing.price.toFixed(2)}`,
			};
			const existingLine = priceLinesMap.get(drawing.id);
			if (existingLine) {
				existingLine.applyOptions(options);
			} else {
				priceLinesMap.set(drawing.id, candleSeries.createPriceLine(options));
			}
		}

		const trendLineIds = new Set(
			drawings.filter((drawing) => drawing.type === 'trendLine').map((drawing) => drawing.id)
		);

		for (const [id, series] of drawingSeriesMap.entries()) {
			if (!trendLineIds.has(id)) {
				chart.removeSeries(series);
				drawingSeriesMap.delete(id);
			}
		}

		for (const drawing of drawings) {
			if (drawing.type !== 'trendLine') continue;
			const startTime = parseTimestamp(drawing.start.time);
			const endTime = parseTimestamp(drawing.end.time);
			if (!Number.isFinite(startTime) || !Number.isFinite(endTime)) continue;

			let series = drawingSeriesMap.get(drawing.id);
			if (!series) {
				series = chart.addLineSeries({
					color: drawing.color || '#38bdf8',
					lineWidth: 2 as const,
					lineStyle: LineStyle.Solid,
					crosshairMarkerVisible: false,
					lastValueVisible: false,
					priceLineVisible: false,
				});
				drawingSeriesMap.set(drawing.id, series);
			} else {
				series.applyOptions({
					color: drawing.color || '#38bdf8',
				});
			}

			series.setData([
				{ time: startTime as Time, value: drawing.start.price },
				{ time: endTime as Time, value: drawing.end.price },
			]);
		}
	}
</script>

<div class="chart-workspace-shell" class:tool-active={activeTool !== 'cursor'}>
	<div class="chart-workspace" bind:this={chartContainer}></div>
	
	{#if showStrategyInfo}
		<div class="strategy-overlay">
			<div class="strategy-overlay-header">
				<div class="strategy-overlay-title">{strategyName || 'Strategy'}</div>
				{#if strategyMeta}
					<div class="strategy-overlay-meta">{strategyMeta}</div>
				{/if}
			</div>
			{#if strategyParamEntries.length > 0}
				<div class="strategy-overlay-grid">
					{#each strategyParamEntries as [key, value]}
						<div class="strategy-overlay-key" title={key}>{key}</div>
						<div class="strategy-overlay-value" title={String(value)}>{formatParamValue(value)}</div>
					{/each}
				</div>
			{:else}
				<div class="strategy-overlay-empty">No explicit params captured</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.chart-workspace-shell {
		position: relative;
		width: 100%;
		height: 100%;
		min-height: 0;
		min-width: 0;
	}

	.chart-workspace {
		width: 100%;
		height: 100%;
		min-height: 0;
		min-width: 0;
	}

	.chart-workspace-shell.tool-active,
	.chart-workspace-shell.tool-active .chart-workspace {
		cursor: crosshair;
	}

	.strategy-overlay {
		position: absolute;
		top: 12px;
		left: 12px;
		max-width: min(420px, calc(100% - 24px));
		max-height: 45%;
		overflow: auto;
		background: rgba(0, 0, 0, 0.82);
		border: 1px solid #1f2937;
		border-radius: 6px;
		padding: 8px;
		backdrop-filter: blur(2px);
		pointer-events: none;
		z-index: 10;
	}

	.strategy-overlay-header {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin-bottom: 6px;
		border-bottom: 1px solid #111827;
		padding-bottom: 4px;
	}

	.strategy-overlay-title {
		color: #f3f4f6;
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.strategy-overlay-meta {
		color: #9ca3af;
		font-size: 10px;
		font-family: 'JetBrains Mono', monospace;
	}

	.strategy-overlay-grid {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 3px 10px;
	}

	.strategy-overlay-key {
		color: #9ca3af;
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		white-space: nowrap;
	}

	.strategy-overlay-value {
		color: #e5e7eb;
		font-size: 10px;
		font-family: 'JetBrains Mono', monospace;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.strategy-overlay-empty {
		color: #6b7280;
		font-size: 10px;
		font-style: italic;
	}
</style>
