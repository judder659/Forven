<script lang="ts">
	// The series chart: the full history, downsampled by the server, that loads
	// finer bars as you zoom in and more bars as you pan, down to the raw bars.
	// Gaps are marked on the bar before them. Streams draw their first column
	// as a line.
	import { createEventDispatcher, onDestroy, onMount } from 'svelte';
	import {
		createChart,
		CrosshairMode,
		type IChartApi,
		type ISeriesApi,
		type MouseEventParams,
		type SeriesMarker,
		type Time,
	} from 'lightweight-charts';
	import { getSeriesBars, getStreamPoints, type SeriesRef } from '$lib/api/dataManager';
	import type { GapSpan } from '$lib/api/dataManagerTypes';
	import { createRequestGuard, errorMessage } from '$lib/stores/dataManager';
	import { gapMarkers, toChartBars, toSeconds, windowToFetch, type ChartBar } from './seriesChart';

	export let ref: SeriesRef;
	export let first: string;
	export let last: string;
	export let stepSeconds: number;
	export let gaps: GapSpan[] = [];
	/** Zoom to this window (a month picked on the coverage map); bump `focusToken` to apply. */
	export let focus: { start: string; end: string } | null = null;
	export let focusToken = 0;
	/** Bump to go back to the full history. */
	export let resetToken = 0;

	const dispatch = createEventDispatcher<{ view: { resolution: string; raw: boolean; bars: number; start: number; end: number; loading: boolean; error: string } }>();
	const MAX_POINTS = 3000;
	const isCandles = () => (ref.stream ?? 'ohlcv') === 'ohlcv';

	let el: HTMLDivElement;
	let chart: IChartApi | null = null;
	let candles: ISeriesApi<'Candlestick'> | null = null;
	let line: ISeriesApi<'Line'> | null = null;
	let resize: ResizeObserver | null = null;
	let times: number[] = [];
	let bars: ChartBar[] = [];
	let lineColumn = '';
	let resolution = '';
	let raw = false;
	let loadedWindow = { start: 0, end: 0 };
	let legend = '';
	let status = { loading: true, error: '' };
	let settleTimer: ReturnType<typeof setTimeout> | undefined;
	let applying = false;
	let appliedFocus = focusToken;
	let appliedReset = resetToken;
	const guard = createRequestGuard();

	const fmt = (v: number) => (Math.abs(v) >= 1000 ? v.toLocaleString('en-US', { maximumFractionDigits: 2 }) : Math.abs(v) >= 1 ? v.toFixed(4).replace(/\.?0+$/, '') : v.toPrecision(4));
	const iso = (seconds: number) => new Date(seconds * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');

	function emit() {
		dispatch('view', { resolution, raw, bars: times.length, start: loadedWindow.start, end: loadedWindow.end, loading: status.loading, error: status.error });
	}

	async function load(window: { start: number; end: number } | null, keepVisible: { from: number; to: number } | null) {
		const { signal, current } = guard.next();
		status = { loading: true, error: '' };
		emit();
		try {
			const start = window ? iso(window.start) : null;
			const end = window ? iso(window.end) : null;
			let next: ChartBar[];
			let nextResolution: string;
			let nextRaw: boolean;
			if (isCandles()) {
				const result = await getSeriesBars(ref, { start, end, max_points: MAX_POINTS }, signal);
				next = toChartBars(result.bars);
				nextResolution = result.resolution;
				nextRaw = result.raw;
			} else {
				const result = await getStreamPoints(ref.symbol, ref.stream ?? 'funding', { timeframe: ref.timeframe, start, end, max_points: MAX_POINTS }, signal);
				lineColumn = result.columns.find((c) => c !== 'timestamp' && c !== 't') ?? result.columns[0] ?? '';
				next = result.points
					.map((p) => {
						const t = toSeconds(String(p.timestamp ?? p.t ?? ''));
						const v = Number(p[lineColumn]);
						return { time: t, open: v, high: v, low: v, close: v };
					})
					.filter((p) => Number.isFinite(p.time) && Number.isFinite(p.close))
					.sort((a, b) => a.time - b.time);
				nextResolution = result.resolution;
				nextRaw = result.raw;
			}
			if (!current()) return;
			bars = next;
			times = next.map((b) => b.time);
			resolution = nextResolution;
			raw = nextRaw;
			loadedWindow = { start: times[0] ?? 0, end: times.at(-1) ?? 0 };
			draw(keepVisible);
			status = { loading: false, error: '' };
		} catch (error) {
			if (!current()) return;
			status = { loading: false, error: errorMessage(error) };
		}
		emit();
	}

	function draw(keepVisible: { from: number; to: number } | null) {
		if (!chart) return;
		applying = true;
		if (isCandles() && candles) {
			candles.setData(bars.map((b) => ({ ...b, time: b.time as Time })));
			const markers: SeriesMarker<Time>[] = gapMarkers(gaps, times).map((m) => ({
				time: m.time as Time,
				position: 'aboveBar',
				shape: 'arrowDown',
				color: m.kind === 'unfillable' ? '#64748b' : '#f59e0b',
				text: raw ? `${m.bars} missing` : '',
				size: 0.6,
			}));
			candles.setMarkers(markers);
		} else if (line) {
			line.setData(bars.map((b) => ({ time: b.time as Time, value: b.close })));
		}
		if (keepVisible) chart.timeScale().setVisibleRange({ from: keepVisible.from as Time, to: keepVisible.to as Time });
		else chart.timeScale().fitContent();
		setTimeout(() => (applying = false), 50);
	}

	function onVisible() {
		if (applying || !chart || status.loading) return;
		clearTimeout(settleTimer);
		settleTimer = setTimeout(() => {
			const range = chart?.timeScale().getVisibleRange();
			if (!range) return;
			const visible = { from: Number(range.from), to: Number(range.to) };
			const window = windowToFetch(visible, { ...loadedWindow, raw, times }, { first: toSeconds(first), last: toSeconds(last), step: stepSeconds });
			if (window) void load(window, visible);
		}, 350);
	}

	function onCrosshair(param: MouseEventParams<Time>) {
		const point = param.time === undefined ? undefined : param.seriesData.get((candles ?? line) as ISeriesApi<'Candlestick'>);
		if (!point) {
			legend = '';
			return;
		}
		const p = point as { open?: number; high?: number; low?: number; close?: number; value?: number };
		const when = iso(Number(param.time)).replace('T', ' ').replace(':00Z', ' UTC');
		legend = p.value != null ? `${when}   ${lineColumn} ${fmt(p.value)}` : `${when}   O ${fmt(p.open ?? 0)}  H ${fmt(p.high ?? 0)}  L ${fmt(p.low ?? 0)}  C ${fmt(p.close ?? 0)}`;
	}

	onMount(() => {
		chart = createChart(el, {
			width: el.clientWidth,
			height: el.clientHeight,
			layout: { background: { color: '#000000' }, textColor: '#666', fontFamily: 'JetBrains Mono, Consolas, monospace', fontSize: 10 },
			grid: { vertLines: { color: '#0d0d0d' }, horzLines: { color: '#0d0d0d' } },
			crosshair: { mode: CrosshairMode.Normal },
			rightPriceScale: { borderColor: '#1a1a1a' },
			timeScale: { borderColor: '#1a1a1a', timeVisible: true, secondsVisible: false },
			localization: { timeFormatter: (t: Time) => iso(Number(t)).slice(0, 16).replace('T', ' ') },
			handleScroll: { vertTouchDrag: false },
		});
		if (isCandles()) {
			candles = chart.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
		} else {
			line = chart.addLineSeries({ color: '#9ca3af', lineWidth: 1, priceLineVisible: false });
		}
		chart.timeScale().subscribeVisibleTimeRangeChange(onVisible);
		chart.subscribeCrosshairMove(onCrosshair);
		resize = new ResizeObserver(() => chart?.applyOptions({ width: el.clientWidth, height: el.clientHeight }));
		resize.observe(el);
		void load(null, null);
	});

	onDestroy(() => {
		clearTimeout(settleTimer);
		guard.cancel();
		resize?.disconnect();
		chart?.remove();
		chart = null;
	});

	$: if (chart && focusToken !== appliedFocus && focus) {
		appliedFocus = focusToken;
		const window = { start: toSeconds(focus.start), end: toSeconds(focus.end) };
		void load(window, { from: window.start, to: window.end });
	}
	$: if (chart && resetToken !== appliedReset) {
		appliedReset = resetToken;
		void load(null, null);
	}
</script>

<div class="relative h-full w-full">
	<div bind:this={el} class="h-full w-full" data-testid="series-chart"></div>
	{#if legend}<div class="pointer-events-none absolute left-2 top-1.5 z-10 truncate font-mono text-[10px] text-[#8a8a8a]">{legend}</div>{/if}
	{#if status.loading}<div class="pointer-events-none absolute right-16 top-1.5 z-10 text-[10px] uppercase tracking-wider text-[#666]">Loading…</div>{/if}
	{#if status.error && !times.length}
		<div class="absolute inset-0 flex items-center justify-center text-[12px] text-red-400">Could not load the chart: {status.error}</div>
	{:else if !status.loading && !times.length}
		<div class="absolute inset-0 flex items-center justify-center text-[12px] text-[#555]">No bars in this range.</div>
	{/if}
</div>
