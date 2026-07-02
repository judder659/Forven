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
	// Full-history strategy trigger points (entry/exit signals, incl. pre-live) —
	// rendered as dim circles, distinct from the solid trade arrows.
	export let triggerMarkers: SignalMarker[] = [];
	// Active position levels (stop / take-profit / trailing) drawn as horizontal lines.
	export let priceLines: Array<{ id: string; price: number; color?: string; title?: string; dashed?: boolean }> = [];

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
	let positionLinesMap = new Map<string, IPriceLine>();

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
	$: if (chart && (entryMarkers || exitMarkers || triggerMarkers)) updateMarkers();
	$: if (chart && (mainIndicators || subIndicators)) updateIndicators();
	$: if (chart && drawings) updateDrawings();
	$: if (chart && priceLines) updatePositionLines();
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
		updatePositionLines();

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

	function markerText(marker: SignalMarker): string | undefined {
		// Real fills get a short label (BUY/SELL/SHORT/COVER) so the four otherwise-
		// colliding pairs are tellable apart. Would-be triggers stay text-free —
		// there can be many and the text would stack and overlap badly.
		if (markerSource(marker) !== 'trade') return undefined;
		return marker.label || undefined;
	}

	// One real-fill marker. Defaults derive the four distinct visuals from
	// direction + entry/exit; the backend's self-describing fields (side → above/
	// below bar, color, shape, label) override when present.
	function buildTradeMarker(m: SignalMarker, leg: 'entry' | 'exit'): SeriesMarker<Time> | null {
		const raw = parseTimestamp(m.timestamp);
		if (isNaN(raw)) return null;
		const t = snapToBar(raw);
		if (t === null) return null;
		const direction = markerDirection(m);
		let position: 'aboveBar' | 'belowBar';
		let color: string;
		let shape: 'arrowUp' | 'arrowDown';
		let label: string;
		if (leg === 'entry') {
			if (direction === 'short') { position = 'aboveBar'; color = '#f97316'; shape = 'arrowDown'; label = 'SHORT'; }
			else { position = 'belowBar'; color = '#22c55e'; shape = 'arrowUp'; label = 'BUY'; }
		} else if (direction === 'short') {
			position = 'belowBar'; color = '#14b8a6'; shape = 'arrowUp'; label = 'COVER';
		} else {
			position = 'aboveBar'; color = '#ef4444'; shape = 'arrowDown'; label = 'SELL';
		}
		const side = String(m.side || '').toLowerCase();
		if (side === 'bull') position = 'belowBar';
		else if (side === 'bear') position = 'aboveBar';
		if (m.color) color = m.color;
		if (m.shape === 'arrowUp' || m.shape === 'arrowDown') shape = m.shape;
		const text = markerText(m) ?? label;
		return { time: t, position, color, shape, ...(text ? { text } : {}) };
	}

	function updateMarkers() {
		if (!candleSeries || !data || data.length === 0) return;

		const markers: SeriesMarker<Time>[] = [];

		// Real fills → four DISTINCT labeled markers (BUY / SELL / SHORT / COVER) so
		// the pairs that used to collide (BUY≡COVER, SELL≡SHORT) are tellable apart.
		// Backend self-describing fields (side/color/shape/label) drive the visual when
		// present; otherwise we derive from direction + entry/exit (backward-compatible).
		for (const m of entryMarkers) {
			const mk = buildTradeMarker(m, 'entry');
			if (mk) markers.push(mk);
		}
		for (const m of exitMarkers) {
			const mk = buildTradeMarker(m, 'exit');
			if (mk) markers.push(mk);
		}

		// Full-history strategy triggers — smaller, MUTED arrows (green up = entry,
		// red down = exit) so they stay visually distinct from the solid trade arrows
		// but still read as long/short signals. No text (there can be many).
		for (const m of triggerMarkers) {
			const raw = parseTimestamp(m.timestamp);
			if (isNaN(raw)) continue;
			const t = snapToBar(raw);
			if (t === null) continue;
			const isExit = m.type === 'exit';
			let position: 'aboveBar' | 'belowBar' = isExit ? 'aboveBar' : 'belowBar';
			let color = isExit ? '#f87171' : '#4ade80';
			let shape: 'arrowUp' | 'arrowDown' = isExit ? 'arrowDown' : 'arrowUp';
			const side = String(m.side || '').toLowerCase();
			if (side === 'bull') position = 'belowBar';
			else if (side === 'bear') position = 'aboveBar';
			if (m.color) color = m.color;
			if (m.shape === 'arrowUp' || m.shape === 'arrowDown') shape = m.shape;
			markers.push({ time: t, position, color, shape, size: 0.7 });
		}

		// Lightweight charts requires markers to be sorted by time
		markers.sort((a, b) => (a.time as number) - (b.time as number));
		candleSeries.setMarkers(markers);
	}

	function updatePositionLines() {
		if (!chart || !candleSeries) return;
		const ids = new Set(priceLines.map((l) => l.id));
		for (const [id, line] of positionLinesMap.entries()) {
			if (!ids.has(id)) {
				candleSeries.removePriceLine(line);
				positionLinesMap.delete(id);
			}
		}
		// Industry-standard active-order representation: a full-width price line with
		// an axis price label (solid ENTRY, dashed SL/TP/Trail). lightweight-charts has
		// no native entry-anchored ray; for a TRUE ray (segment from entry bar → now)
		// draw a 2-point line series instead — see the trendLine pattern below
		// (chart.addLineSeries(...).setData([{ time: start }, { time: now }])).
		for (const level of priceLines) {
			if (!Number.isFinite(level.price)) continue;
			const options = {
				price: level.price,
				color: level.color || '#f59e0b',
				lineWidth: 2 as const,
				lineStyle: level.dashed ? LineStyle.Dashed : LineStyle.Solid,
				axisLabelVisible: true,
				title: level.title || `@ ${level.price.toFixed(2)}`,
			};
			const existing = positionLinesMap.get(level.id);
			if (existing) existing.applyOptions(options);
			else positionLinesMap.set(level.id, candleSeries.createPriceLine(options));
		}
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
		border: 1px solid #222;
		padding: 8px;
		pointer-events: none;
		z-index: 10;
	}

	.strategy-overlay-header {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin-bottom: 6px;
		border-bottom: 1px solid #1a1a1a;
		padding-bottom: 4px;
	}

	.strategy-overlay-title {
		color: #fff;
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.strategy-overlay-meta {
		color: #666;
		font-size: 10px;
		font-family: 'JetBrains Mono', monospace;
	}

	.strategy-overlay-grid {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 3px 10px;
	}

	.strategy-overlay-key {
		color: #666;
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
		color: #666;
		font-size: 10px;
		font-style: italic;
	}
</style>
