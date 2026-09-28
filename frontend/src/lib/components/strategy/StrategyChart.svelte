<script lang="ts">
	// The Strategy Creator's chart: price with the preview's trades, the
	// out-of-sample region and entry-rule shading on top, and one synced pane
	// (own scale, own axis) per indicator that draws below price. Click a trade
	// to select it. lightweight-charts 4 has no native panes, so each pane is a
	// chart whose time scale follows the price chart's logical range.
	import { createEventDispatcher, onDestroy, onMount } from 'svelte';
	import {
		createChart,
		CrosshairMode,
		LineStyle,
		type IChartApi,
		type IPriceLine,
		type ISeriesApi,
		type LogicalRange,
		type MouseEventParams,
		type SeriesMarker,
		type Time,
	} from 'lightweight-charts';
	import type { BacktestChartIndicator, OHLCVBar, PreviewTrade, RuleSideKey } from '$lib/api';
	import { formatValue } from '$lib/utils/ruleLabels';

	export let bars: OHLCVBar[] = [];
	export let mainIndicators: BacktestChartIndicator[] = [];
	export let subIndicators: BacktestChartIndicator[] = [];
	export let trades: PreviewTrade[] = [];
	export let selectedTrade: number | null = null;
	export let ruleSpans: Partial<Record<RuleSideKey, [string, string][]>> = {};
	export let oosStart: string | null = null;
	export let showRuleShading = true;
	/** Display names by indicator id ("RSI(14)") and by series name. */
	export let labels: Record<string, string> = {};
	/** Horizontal reference lines per indicator id, e.g. the RSI level a rule compares with. */
	export let thresholds: Record<string, Array<{ value: number; label: string }>> = {};
	/** Bump to re-frame the latest bars (new market or window). */
	export let fitToken = 0;
	/** Bump to scroll the selected trade into view. */
	export let focusToken = 0;

	const dispatch = createEventDispatcher<{ select: number | null }>();
	const DEFAULT_VISIBLE_BARS = 400;
	const CHART_OPTIONS = {
		layout: { background: { color: '#000000' }, textColor: '#666', fontFamily: 'JetBrains Mono, Consolas, monospace', fontSize: 10 },
		grid: { vertLines: { color: '#0d0d0d' }, horzLines: { color: '#0d0d0d' } },
		crosshair: { mode: CrosshairMode.Normal },
		rightPriceScale: { borderColor: '#1a1a1a', minimumWidth: 72 },
		handleScroll: { vertTouchDrag: false },
	};

	let mainEl: HTMLDivElement;
	let chart: IChartApi | null = null;
	let candles: ISeriesApi<'Candlestick'> | null = null;
	let oosShade: ISeriesApi<'Histogram'> | null = null;
	let ruleShade: ISeriesApi<'Histogram'> | null = null;
	let overlaySeries = new Map<string, ISeriesApi<'Line'>>();
	let tradeLines: IPriceLine[] = [];
	let resizeObserver: ResizeObserver | null = null;
	let times: number[] = [];
	let timeIndex = new Map<number, number>();
	let appliedFit = -1;
	let appliedFocus = focusToken;
	let syncing = false;
	let lastCrosshairTime: Time | undefined | null = null;
	let legend = '';
	let oosX: number | null = null;

	type Pane = {
		id: string;
		chart: IChartApi;
		anchor: ISeriesApi<'Line'>;
		series: Map<string, ISeriesApi<'Line'> | ISeriesApi<'Histogram'>>;
		lines: IPriceLine[];
		legend: string;
	};
	let panes: Pane[] = [];
	let paneLegends: Record<string, string> = {};

	const toTime = (iso: string) => Math.floor(new Date(iso).getTime() / 1000);

	$: groups = Array.from(
		subIndicators.reduce((map, line) => {
			const id = String(line.group ?? line.name);
			map.set(id, [...(map.get(id) ?? []), line]);
			return map;
		}, new Map<string, BacktestChartIndicator[]>()),
	).map(([id, lines]) => ({ id, lines }));

	function bandOf(time: number): number | undefined {
		return timeIndex.get(time);
	}

	// ---- Price chart -------------------------------------------------------------
	function setBars() {
		if (!candles) return;
		const rows = bars
			.map((bar) => ({ time: toTime(bar.timestamp), open: bar.open, high: bar.high, low: bar.low, close: bar.close }))
			.filter((row) => Number.isFinite(row.time))
			.sort((a, b) => a.time - b.time);
		times = rows.map((row) => row.time);
		timeIndex = new Map(times.map((time, index) => [time, index]));
		candles.setData(rows.map((row) => ({ ...row, time: row.time as Time })));
	}

	function setOverlays() {
		if (!chart) return;
		const wanted = new Set(mainIndicators.map((line) => line.name));
		for (const [name, series] of overlaySeries) {
			if (!wanted.has(name)) {
				chart.removeSeries(series);
				overlaySeries.delete(name);
			}
		}
		for (const line of mainIndicators) {
			let series = overlaySeries.get(line.name);
			if (!series) {
				series = chart.addLineSeries({ color: line.color || '#22d3ee', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
				overlaySeries.set(line.name, series);
			} else {
				series.applyOptions({ color: line.color || '#22d3ee' });
			}
			series.setData(line.data.filter((pt) => pt.value != null && Number.isFinite(pt.value)).map((pt) => ({ time: toTime(pt.timestamp) as Time, value: pt.value as number })));
		}
	}

	function setShading() {
		if (!oosShade || !ruleShade) return;
		const oos = oosStart ? toTime(oosStart) : Infinity;
		oosShade.setData(times.map((time) => (time >= oos ? { time: time as Time, value: 1 } : { time: time as Time })));
		oosX = null;
		const spans: Array<{ from: number; to: number; color: string }> = [];
		if (showRuleShading) {
			for (const [side, color] of [['entry_long', 'rgba(34,197,94,0.10)'], ['entry_short', 'rgba(249,115,22,0.10)']] as const) {
				for (const [from, to] of ruleSpans[side] ?? []) spans.push({ from: toTime(from), to: toTime(to), color });
			}
		}
		spans.sort((a, b) => a.from - b.from);
		const data: Array<{ time: Time; value?: number; color?: string }> = [];
		let cursor = 0;
		for (const time of times) {
			while (cursor < spans.length && spans[cursor].to < time) cursor++;
			const span = spans[cursor];
			data.push(span && span.from <= time && time <= span.to ? { time: time as Time, value: 1, color: span.color } : { time: time as Time });
		}
		ruleShade.setData(data);
		positionOosBadge();
	}

	function positionOosBadge() {
		if (!chart || !oosStart || !mainEl) {
			oosX = null;
			return;
		}
		const x = chart.timeScale().timeToCoordinate(toTime(oosStart) as Time);
		// Negative: the boundary is off to the left and every visible bar is out-of-sample.
		oosX = x === null || x > mainEl.clientWidth - 80 ? null : Math.round(x);
	}

	function setMarkers() {
		if (!candles) return;
		const markers: SeriesMarker<Time>[] = [];
		for (const trade of trades) {
			const selected = trade.n === selectedTrade;
			const long = trade.direction === 'long';
			markers.push({
				time: toTime(trade.entry_time) as Time,
				position: long ? 'belowBar' : 'aboveBar',
				shape: long ? 'arrowUp' : 'arrowDown',
				color: selected ? '#ffffff' : long ? '#22c55e' : '#f97316',
				size: selected ? 1.6 : 1,
				...(selected ? { text: `#${trade.n}` } : {}),
			});
			markers.push({
				time: toTime(trade.exit_time) as Time,
				position: long ? 'aboveBar' : 'belowBar',
				shape: 'circle',
				color: selected ? '#ffffff' : trade.pnl_pct >= 0 ? '#16a34a' : '#dc2626',
				size: selected ? 1.2 : 0.6,
			});
		}
		markers.sort((a, b) => (a.time as number) - (b.time as number));
		candles.setMarkers(markers);
		for (const line of tradeLines) candles.removePriceLine(line);
		tradeLines = [];
		const trade = trades.find((t) => t.n === selectedTrade);
		if (trade) {
			tradeLines.push(candles.createPriceLine({ price: trade.entry_price, color: '#e5e5e5', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'entry' }));
			tradeLines.push(candles.createPriceLine({ price: trade.exit_price, color: trade.pnl_pct >= 0 ? '#22c55e' : '#ef4444', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'exit' }));
		}
	}

	function frameLatest() {
		if (!chart || !times.length) return;
		const to = times.length - 1 + 3;
		chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, times.length - DEFAULT_VISIBLE_BARS), to });
	}

	function focusSelected() {
		const trade = trades.find((t) => t.n === selectedTrade);
		if (!chart || !trade) return;
		const from = bandOf(toTime(trade.entry_time));
		const to = bandOf(toTime(trade.exit_time));
		if (from === undefined || to === undefined) return;
		const pad = Math.max(30, Math.round((to - from) * 1.5));
		chart.timeScale().setVisibleLogicalRange({ from: from - pad, to: to + pad });
	}

	function onClick(param: MouseEventParams<Time>) {
		if (param.time === undefined) return;
		const index = bandOf(param.time as number);
		if (index === undefined) return;
		let best: PreviewTrade | null = null;
		let bestDistance = Infinity;
		for (const trade of trades) {
			const from = bandOf(toTime(trade.entry_time));
			const to = bandOf(toTime(trade.exit_time));
			if (from === undefined || to === undefined) continue;
			const distance = index >= from && index <= to ? 0 : Math.min(Math.abs(index - from), Math.abs(index - to));
			if (distance < bestDistance) {
				best = trade;
				bestDistance = distance;
			}
		}
		dispatch('select', best && bestDistance <= 3 ? best.n : null);
	}

	function valueAt(series: ISeriesApi<'Line'> | ISeriesApi<'Histogram'>, index: number): number | undefined {
		const point = series.dataByIndex(index) as { value?: number } | null;
		return point?.value;
	}

	function updateLegends(time: Time | undefined) {
		const index = time === undefined ? undefined : bandOf(time as number);
		if (index === undefined) {
			legend = '';
			paneLegends = {};
			return;
		}
		const bar = bars[index];
		const parts = bar ? [`O ${formatValue(bar.open)}`, `H ${formatValue(bar.high)}`, `L ${formatValue(bar.low)}`, `C ${formatValue(bar.close)}`] : [];
		for (const [name, series] of overlaySeries) {
			const value = valueAt(series, index);
			if (value !== undefined) parts.push(`${labels[name] ?? name} ${formatValue(value)}`);
		}
		legend = parts.join('   ');
		const next: Record<string, string> = {};
		for (const pane of panes) {
			next[pane.id] = [...pane.series]
				.map(([name, series]) => [name, valueAt(series, index)] as const)
				.filter(([, value]) => value !== undefined)
				.map(([name, value]) => `${labels[name] ?? name} ${formatValue(value)}`)
				.join('   ');
		}
		paneLegends = next;
	}

	// ---- Indicator panes ---------------------------------------------------------
	function syncRange(source: IChartApi, range: LogicalRange | null) {
		if (syncing || !range) return;
		syncing = true;
		for (const target of [chart, ...panes.map((pane) => pane.chart)]) {
			if (target && target !== source) target.timeScale().setVisibleLogicalRange(range);
		}
		syncing = false;
		positionOosBadge();
	}

	/** Mirror the crosshair onto the other charts. The library echoes a programmatic
	 * move as an event, so a time already mirrored is not mirrored again. */
	function syncCrosshair(source: IChartApi, param: MouseEventParams<Time>) {
		const time = param.time;
		if (time === lastCrosshairTime) return;
		lastCrosshairTime = time;
		updateLegends(time);
		const index = time === undefined ? undefined : bandOf(time as number);
		for (const target of [chart, ...panes.map((pane) => pane.chart)]) {
			if (!target || target === source) continue;
			if (time === undefined || index === undefined) {
				target.clearCrosshairPosition();
				continue;
			}
			if (target === chart && candles && bars[index]) {
				target.setCrosshairPosition(bars[index].close, time, candles);
				continue;
			}
			const pane = panes.find((p) => p.chart === target);
			const first = pane ? [...pane.series.values()][0] : undefined;
			const value = first ? valueAt(first, index) : undefined;
			if (pane && first && value !== undefined) pane.chart.setCrosshairPosition(value, time, first);
			else target.clearCrosshairPosition();
		}
	}

	function fillPane(pane: Pane, lines: BacktestChartIndicator[]) {
		pane.anchor.setData(times.map((time) => ({ time: time as Time })));
		const wanted = new Set(lines.map((line) => line.name));
		for (const [name, series] of pane.series) {
			if (!wanted.has(name)) {
				pane.chart.removeSeries(series);
				pane.series.delete(name);
			}
		}
		for (const line of lines) {
			const histogram = line.name.endsWith('_hist');
			let series = pane.series.get(line.name);
			if (!series) {
				series = histogram
					? pane.chart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false })
					: pane.chart.addLineSeries({ color: line.color || '#22d3ee', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false });
				pane.series.set(line.name, series);
			}
			const points = line.data.filter((pt) => pt.value != null && Number.isFinite(pt.value));
			if (histogram) {
				(series as ISeriesApi<'Histogram'>).setData(points.map((pt) => ({ time: toTime(pt.timestamp) as Time, value: pt.value as number,
					color: (pt.value as number) >= 0 ? 'rgba(34,197,94,0.45)' : 'rgba(239,68,68,0.45)' })));
			} else {
				(series as ISeriesApi<'Line'>).setData(points.map((pt) => ({ time: toTime(pt.timestamp) as Time, value: pt.value as number })));
			}
		}
		const first = pane.series.values().next().value;
		for (const line of pane.lines) first?.removePriceLine(line);
		pane.lines = (thresholds[pane.id] ?? []).flatMap((level) =>
			first ? [first.createPriceLine({ price: level.value, color: '#737373', lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: true, title: level.label })] : [],
		);
	}

	function paneAction(node: HTMLDivElement, group: { id: string; lines: BacktestChartIndicator[] }) {
		// The price chart carries the TradingView attribution once for the stack.
		const paneChart = createChart(node, {
			...CHART_OPTIONS, layout: { ...CHART_OPTIONS.layout, attributionLogo: false },
			width: node.clientWidth, height: node.clientHeight, timeScale: { visible: false, borderColor: '#1a1a1a' },
		});
		const pane: Pane = { id: group.id, chart: paneChart, anchor: paneChart.addLineSeries({ visible: false }), series: new Map(), lines: [], legend: '' };
		panes = [...panes, pane];
		fillPane(pane, group.lines);
		const range = chart?.timeScale().getVisibleLogicalRange();
		if (range) paneChart.timeScale().setVisibleLogicalRange(range);
		const onRange = (r: LogicalRange | null) => syncRange(paneChart, r);
		const onMove = (param: MouseEventParams<Time>) => syncCrosshair(paneChart, param);
		paneChart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
		paneChart.subscribeCrosshairMove(onMove);
		paneChart.subscribeClick(onClick);
		const observer = new ResizeObserver((entries) => {
			const { width, height } = entries[0].contentRect;
			paneChart.applyOptions({ width, height });
		});
		observer.observe(node);
		return {
			update(next: { id: string; lines: BacktestChartIndicator[] }) {
				fillPane(pane, next.lines);
			},
			destroy() {
				observer.disconnect();
				paneChart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
				paneChart.unsubscribeCrosshairMove(onMove);
				paneChart.unsubscribeClick(onClick);
				panes = panes.filter((p) => p !== pane);
				paneChart.remove();
			},
		};
	}

	onMount(() => {
		chart = createChart(mainEl, { ...CHART_OPTIONS, width: mainEl.clientWidth, height: mainEl.clientHeight, timeScale: { borderColor: '#1a1a1a', timeVisible: true, secondsVisible: false } });
		// Shading first so it draws behind the candles.
		oosShade = chart.addHistogramSeries({ priceScaleId: 'shade-oos', color: 'rgba(255,255,255,0.035)', priceLineVisible: false, lastValueVisible: false });
		ruleShade = chart.addHistogramSeries({ priceScaleId: 'shade-rule', priceLineVisible: false, lastValueVisible: false });
		for (const id of ['shade-oos', 'shade-rule']) chart.priceScale(id).applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
		candles = chart.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
		chart.subscribeClick(onClick);
		chart.subscribeCrosshairMove((param) => syncCrosshair(chart!, param));
		chart.timeScale().subscribeVisibleLogicalRangeChange((range) => syncRange(chart!, range));
		resizeObserver = new ResizeObserver((entries) => {
			const { width, height } = entries[0].contentRect;
			chart?.applyOptions({ width, height });
			positionOosBadge();
		});
		resizeObserver.observe(mainEl);
		refresh();
	});

	onDestroy(() => {
		resizeObserver?.disconnect();
		chart?.remove();
		chart = null;
	});

	function refresh() {
		if (!chart) return;
		setBars();
		setOverlays();
		setShading();
		setMarkers();
		for (const pane of panes) fillPane(pane, groups.find((group) => group.id === pane.id)?.lines ?? []);
		if (fitToken !== appliedFit && times.length) {
			appliedFit = fitToken;
			frameLatest();
		}
	}

	$: if (chart) (bars, mainIndicators, subIndicators, oosStart, thresholds, refresh());
	$: if (chart) (trades, selectedTrade, setMarkers());
	$: if (chart) (ruleSpans, showRuleShading, setShading());
	$: if (chart && focusToken !== appliedFocus) {
		appliedFocus = focusToken;
		focusSelected();
	}
</script>

<div class="flex h-full min-h-0 flex-col bg-black">
	<div class="relative min-h-[220px] flex-1" bind:this={mainEl}>
		{#if legend}
			<div class="pointer-events-none absolute left-2 top-1.5 z-10 truncate text-[10px] text-[#8a8a8a]">{legend}</div>
		{/if}
		{#if oosX !== null && oosX < 0}
			<div class="pointer-events-none absolute left-2 top-5 z-10 whitespace-nowrap bg-black/70 px-1 text-[9px] uppercase tracking-wider text-[#777]">out-of-sample</div>
		{:else if oosX !== null}
			<div class="pointer-events-none absolute top-0 z-10 h-full border-l border-dashed border-[#333]" style="left: {oosX}px">
				<span class="ml-1 mt-5 inline-block whitespace-nowrap bg-black/70 px-1 text-[9px] uppercase tracking-wider text-[#777]">out-of-sample →</span>
			</div>
		{/if}
	</div>
	{#each groups as group (group.id)}
		<div class="relative h-[92px] shrink-0 border-t border-[#1a1a1a]" use:paneAction={group}>
			<div class="pointer-events-none absolute left-2 top-1 z-10 truncate text-[10px]">
				<span class="text-[#bbb]">{labels[group.id] ?? group.id}</span>
				<span class="ml-2 text-[#777]">{paneLegends[group.id] ?? ''}</span>
			</div>
		</div>
	{/each}
</div>
