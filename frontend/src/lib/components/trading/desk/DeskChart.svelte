<script lang="ts">
	/**
	 * The selected strategy's market: candles on any timeframe, its fills, the
	 * strategy's own signals, refused entries/exits from the journal, and the open
	 * legs' entry/stop/target lines. The forming candle follows the price stream.
	 */
	import { onDestroy } from 'svelte';
	import { getPaperSessionChart, type OHLCVBar, type TradeMarker } from '$lib/api';
	import type { AssetMarketContext, JournalEvent } from '$lib/api/desk';
	import type { PaperTradingSession } from '$lib/api/paper';
	import type { IndicatorConfig, SignalMarker } from '$lib/stores/chartStore';
	import type { ChartDrawing, ChartDrawingPoint, ChartDrawingTool } from '$lib/components/chart/types';
	import ChartWorkspace from '$lib/components/chart/ChartWorkspace.svelte';
	import RegimeStripe from '$lib/components/regime/RegimeStripe.svelte';
	import { ORDERED_TIMEFRAME_VALUES } from '$lib/config/timeframes';
	import { applyTickToBars } from '$lib/utils/liveBars';
	import { dur, fmtPct, fmtPx, nextBarClose, parseTs, toneClass } from '$lib/utils/tradingDesk/format';
	import { atr as atrOf, type Leg } from '$lib/utils/tradingDesk/position';
	import DeskMarketStrip from './DeskMarketStrip.svelte';

	export let session: PaperTradingSession;
	export let name = '';
	export let legs: Leg[] = [];
	export let refusals: JournalEvent[] = [];
	export let market: AssetMarketContext | null = null;
	export let asset = '';
	export let livePrice: number | null = null;
	export let refreshToken = 0;
	export let now = Date.now();
	/** ATR(14) on the strategy's own timeframe, for the ticket's suggested stop. */
	export let atr: number | null = null;

	const TIMEFRAMES = ORDERED_TIMEFRAME_VALUES.filter((tf) => tf !== '1w');
	const POLL_MS = 15_000;

	let timeframe = session.timeframe || '1h';
	let loadedKey = '';
	let bars: OHLCVBar[] = [];
	let loading = false;
	let loadError: string | null = null;
	let mainIndicators: IndicatorConfig[] = [];
	let subIndicators: IndicatorConfig[] = [];
	let entryMarkers: SignalMarker[] = [];
	let exitMarkers: SignalMarker[] = [];
	let triggerMarkers: SignalMarker[] = [];
	let trailLevels: number[] = [];
	let showFills = true;
	let showSignals = true;
	let showRefused = true;
	let showIndicators = true;
	let showRegimes = false;
	let activeTool: ChartDrawingTool = 'cursor';
	let drawings: ChartDrawing[] = [];
	let pendingStart: ChartDrawingPoint | null = null;
	let fitContentToken = 0;
	let generation = 0;
	let timer: ReturnType<typeof setInterval> | null = null;
	let lastSessionId = session.id;

	$: if (session.id !== lastSessionId) {
		lastSessionId = session.id;
		timeframe = session.timeframe || '1h';
		atr = null;
		drawings = [];
		pendingStart = null;
		activeTool = 'cursor';
		bars = [];
		fitContentToken += 1;
	}
	$: key = `${session.id}|${timeframe}`;
	$: if (key !== loadedKey) {
		loadedKey = key;
		bars = [];
		void load();
	}
	// A fill or manual action elsewhere on the page asks for fresh markers.
	let seenRefresh = refreshToken;
	$: if (refreshToken !== seenRefresh) {
		seenRefresh = refreshToken;
		void load();
	}
	$: if (livePrice !== null && bars.length && session.mode !== 'replay') {
		const next = applyTickToBars(bars, livePrice, Date.now(), timeframe);
		if (next !== bars) bars = next;
	}

	function toSignalMarker(marker: TradeMarker, type: 'entry' | 'exit'): SignalMarker {
		const legacy = marker as TradeMarker & { time?: string };
		return {
			timestamp: legacy.time ?? marker.timestamp,
			price: marker.price,
			type,
			direction: String(marker.direction || 'long').toLowerCase() === 'short' ? 'short' : 'long',
			label: marker.label ?? undefined,
			source: String(marker.marker_kind || 'trade').toLowerCase() === 'signal' ? 'signal' : 'trade',
			...(marker.side ? { side: marker.side } : {}),
			...(marker.shape ? { shape: marker.shape } : {}),
			...(marker.color ? { color: marker.color } : {}),
		};
	}

	async function load(): Promise<void> {
		const thisGeneration = ++generation;
		const sessionId = session.id;
		const tf = timeframe;
		if (!bars.length) loading = true;
		try {
			const bundle = await getPaperSessionChart(sessionId, { limit: 2000, timeframe: tf });
			if (thisGeneration !== generation) return;
			let nextBars = [...(bundle.bars ?? [])];
			if (livePrice !== null && nextBars.length) nextBars = applyTickToBars(nextBars, livePrice, Date.now(), tf);
			bars = nextBars;
			if (tf === (session.timeframe || '1h')) atr = atrOf(nextBars, 14);
			const toConfig = (series: { name: string; panel: string; color?: string; data: Array<{ timestamp: string; value: number | null }> }): IndicatorConfig => ({
				id: series.name,
				name: series.name,
				params: {},
				color: series.color || '#aab1bc',
				panel: series.panel === 'main' ? 'main' : 'sub1',
				visible: true,
				data: series.data.filter((point) => point.value !== null && point.value !== undefined).map((point) => ({ timestamp: point.timestamp, value: point.value as number })),
			});
			mainIndicators = (bundle.main_indicators ?? []).map(toConfig);
			subIndicators = (bundle.sub_indicators ?? []).map(toConfig);
			entryMarkers = (bundle.entry_markers ?? []).map((marker) => toSignalMarker(marker, 'entry'));
			exitMarkers = (bundle.exit_markers ?? []).map((marker) => toSignalMarker(marker, 'exit'));
			triggerMarkers = [
				...(bundle.trigger_entries ?? []).map((marker) => toSignalMarker(marker, 'entry')),
				...(bundle.trigger_exits ?? []).map((marker) => toSignalMarker(marker, 'exit')),
			];
			trailLevels = (bundle.active_levels?.trail ?? []).map((level) => level.price).filter((price) => Number.isFinite(price));
			loadError = null;
		} catch (error) {
			if (thisGeneration !== generation) return;
			loadError = error instanceof Error ? error.message : 'The chart could not load.';
		} finally {
			if (thisGeneration === generation) loading = false;
		}
	}

	timer = setInterval(() => {
		if (typeof document !== 'undefined' && document.hidden) return;
		void load();
	}, POLL_MS);
	onDestroy(() => {
		if (timer) clearInterval(timer);
		generation += 1;
	});

	$: price = livePrice ?? (Number.isFinite(session.current_price) && session.current_price > 0 ? session.current_price : null);
	$: dayAgo = (() => {
		if (!bars.length) return null;
		const cutoff = parseTs(bars[bars.length - 1].timestamp);
		if (cutoff === null) return null;
		const target = cutoff - 86_400_000;
		for (let i = bars.length - 1; i >= 0; i -= 1) {
			const t = parseTs(bars[i].timestamp);
			if (t !== null && t <= target) return i;
		}
		return null;
	})();
	$: dayBars = dayAgo !== null ? bars.slice(dayAgo + 1) : bars;
	$: change24h = dayAgo !== null && price !== null ? ((price - bars[dayAgo].close) / bars[dayAgo].close) * 100 : null;
	$: high24h = dayBars.length ? Math.max(...dayBars.map((bar) => bar.high)) : null;
	$: low24h = dayBars.length ? Math.min(...dayBars.map((bar) => bar.low)) : null;

	$: priceLines = [
		...legs.flatMap((leg, index) => [
			{ id: `entry-${index}`, price: leg.entry, color: '#8fb0ff', title: legs.length > 1 ? `Entry ${leg.side}` : 'Entry', dashed: false },
			...(leg.stop !== null ? [{ id: `stop-${index}`, price: leg.stop, color: '#e0663f', title: 'Stop', dashed: true }] : []),
			...(leg.takeProfit !== null ? [{ id: `tp-${index}`, price: leg.takeProfit, color: '#139a9f', title: 'Target', dashed: true }] : []),
		]),
		...trailLevels.map((level, index) => ({ id: `trail-${index}`, price: level, color: '#e7b24a', title: 'Trail', dashed: true })),
	];
	$: refusedMarkers = showRefused
		? refusals.map((event) => ({
			timestamp: event.first_at ?? event.at,
			label: `Refused${(event.count ?? 1) > 1 ? ` ×${event.count}` : ''}`,
			position: event.kind === 'exit_refused' ? ('aboveBar' as const) : ('belowBar' as const),
		}))
		: [];

	function pickTool(tool: Exclude<ChartDrawingTool, 'cursor'>) {
		activeTool = activeTool === tool ? 'cursor' : tool;
		pendingStart = null;
	}

	function onDrawingPoint(event: CustomEvent<ChartDrawingPoint>) {
		if (activeTool === 'cursor') return;
		const point = event.detail;
		const id = `${activeTool}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
		if (activeTool === 'horizontalLine') {
			drawings = [...drawings, { id, type: 'horizontalLine', price: point.price, color: '#e7b24a', label: fmtPx(point.price) }];
			return;
		}
		if (!pendingStart) {
			pendingStart = point;
			return;
		}
		drawings = [...drawings, { id, type: 'trendLine', start: pendingStart, end: point, color: '#8fb0ff' }];
		pendingStart = null;
	}

	const chip = (on: boolean) =>
		`rounded-sm border px-2 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] transition-colors ${on ? 'border-sc-ink4 bg-sc-raise text-sc-ink' : 'border-sc-line bg-sc-panel text-sc-ink2 hover:border-sc-line2 hover:text-sc-ink'}`;
</script>

<section class="flex min-w-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label="Chart" data-testid="desk-chart">
	<header class="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-sc-line px-3 pb-2 pt-2.5">
		<div class="grid min-w-0 gap-0.5">
			<div class="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5">
				<b class="text-[15px] font-semibold text-sc-ink">{session.symbol}</b>
				<span class="text-[12px] text-sc-ink3">{session.strategy_id ?? session.strategy_name} · {name} · trades on {session.timeframe}</span>
			</div>
			<div class="flex flex-wrap items-baseline gap-x-3.5 gap-y-0.5">
				<span class="font-plex-mono text-[22px] font-medium text-sc-ink" data-testid="desk-price">{fmtPx(price)}</span>
				<span class={`text-[12px] ${toneClass(change24h)}`}>{fmtPct(change24h)} 24h</span>
				<span class="text-[12px] text-sc-ink3">24h high <b class="font-plex-mono font-medium text-sc-ink2">{fmtPx(high24h)}</b></span>
				<span class="text-[12px] text-sc-ink3">24h low <b class="font-plex-mono font-medium text-sc-ink2">{fmtPx(low24h)}</b></span>
			</div>
		</div>
		<div class="grid justify-items-end gap-1.5 max-sm:justify-items-start">
			<span class="text-[12px] text-sc-ink3">{timeframe} bar closes in <b class="font-plex-mono font-medium text-sc-ink">{dur(nextBarClose(timeframe, now) - now)}</b></span>
			<div class="flex flex-wrap justify-end gap-1" role="group" aria-label="Chart timeframe">
				{#each TIMEFRAMES as tf}
					<button type="button" class={chip(tf === timeframe)} aria-pressed={tf === timeframe} on:click={() => { timeframe = tf; drawings = []; pendingStart = null; fitContentToken += 1; }}>{tf}</button>
				{/each}
			</div>
			<div class="flex flex-wrap justify-end gap-x-3 gap-y-1 text-[12px] text-sc-ink2">
				<label class="inline-flex cursor-pointer items-center gap-1.5"><input type="checkbox" class="accent-sc-ink2" bind:checked={showFills} />Fills</label>
				<label class="inline-flex cursor-pointer items-center gap-1.5"><input type="checkbox" class="accent-sc-ink2" bind:checked={showSignals} />Strategy signals</label>
				<label class="inline-flex cursor-pointer items-center gap-1.5"><input type="checkbox" class="accent-sc-ink2" bind:checked={showRefused} />Refused</label>
				{#if mainIndicators.length || subIndicators.length}
					<label class="inline-flex cursor-pointer items-center gap-1.5"><input type="checkbox" class="accent-sc-ink2" bind:checked={showIndicators} />Indicators</label>
				{/if}
			</div>
		</div>
	</header>
	<DeskMarketStrip {asset} context={market} {now} />
	<div class="flex flex-wrap items-center gap-1.5 border-b border-sc-line px-3 py-1.5 text-[11.5px] text-sc-ink3">
		<button type="button" class={chip(activeTool === 'horizontalLine')} on:click={() => pickTool('horizontalLine')}>H-line</button>
		<button type="button" class={chip(activeTool === 'trendLine')} on:click={() => pickTool('trendLine')}>Trend</button>
		<button type="button" class={chip(false)} on:click={() => (fitContentToken += 1)}>Reset view</button>
		<button type="button" class={chip(false)} disabled={!drawings.length && !pendingStart} on:click={() => { drawings = []; pendingStart = null; activeTool = 'cursor'; }}>Clear</button>
		<span class="ml-1">
			{#if activeTool === 'horizontalLine'}Click the chart to place a level.
			{:else if activeTool === 'trendLine'}{pendingStart ? 'Click a second point to finish the line.' : 'Click a first point to start a line.'}
			{:else}{drawings.length ? `${drawings.length} drawing${drawings.length === 1 ? '' : 's'} on the chart` : ''}{/if}
		</span>
	</div>
	<div class="relative min-h-[440px] flex-1">
		{#if loading && !bars.length}
			<div class="absolute inset-0 grid place-items-center text-[12px] text-sc-ink3">Loading {session.symbol} {timeframe}…</div>
		{:else if loadError && !bars.length}
			<div class="absolute inset-0 grid place-items-center px-6 text-center text-[12px] text-sc-ink3">The chart could not load: {loadError}</div>
		{:else if bars.length}
			<div class="absolute inset-0">
				<ChartWorkspace
					data={bars}
					entryMarkers={showFills ? entryMarkers : []}
					exitMarkers={showFills ? exitMarkers : []}
					triggerMarkers={showSignals ? triggerMarkers : []}
					{refusedMarkers}
					{priceLines}
					mainIndicators={showIndicators ? mainIndicators : []}
					subIndicators={showIndicators ? subIndicators : []}
					strategyName={session.strategy_id ?? session.strategy_name}
					showStrategyInfo={false}
					autoScroll={true}
					windowSize={200}
					{drawings}
					{activeTool}
					{fitContentToken}
					on:drawingPoint={onDrawingPoint}
				/>
			</div>
		{:else}
			<div class="absolute inset-0 grid place-items-center text-[12px] text-sc-ink3">No bars for {session.symbol} {timeframe} yet.</div>
		{/if}
	</div>
	<div class="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-sc-line px-3 py-1.5 text-[12px] text-sc-ink2">
		<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5 rounded-sm bg-[#e7b24a]"></i>Refused entry or exit</span>
		<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0 w-4 border-t-2 border-[#8fb0ff]"></i>Entry</span>
		<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0 w-4 border-t-2 border-dashed border-[#e0663f]"></i>Stop</span>
		<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0 w-4 border-t-2 border-dashed border-[#139a9f]"></i>Target</span>
		<button type="button" class="ml-auto text-sc-ink3 hover:text-sc-ink" on:click={() => (showRegimes = !showRegimes)}>Regimes {showRegimes ? '▾' : '▸'}</button>
	</div>
	{#if showRegimes}
		<div class="border-t border-sc-line bg-sc-bg">
			<RegimeStripe symbol={session.symbol} {timeframe} />
		</div>
	{/if}
</section>
