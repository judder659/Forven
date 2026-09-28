<script lang="ts">
	// One series: can I trust it for this strategy? Freshness and quality from
	// the server, the full history chart, a month-by-month coverage map, gaps,
	// the other streams of the symbol, who reads it, where it came from, and
	// the raw rows.
	import { onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { ApiError, isRouteMissingError } from '$lib/api/core';
	import {
		extendHistory,
		freezeSeries,
		getSeriesBars,
		getSeriesDetail,
		getSeriesRows,
		getStreamPoints,
		refreshSeries,
		type SeriesRef,
	} from '$lib/api/dataManager';
	import type { DataStream, MonthCell, RowsResponse, SeriesDetail, StreamSummary } from '$lib/api/dataManagerTypes';
	import DeleteReview from '$lib/components/data-manager/DeleteReview.svelte';
	import JobRow from '$lib/components/data-manager/JobRow.svelte';
	import MiniLine from '$lib/components/data-manager/MiniLine.svelte';
	import MonthMap from '$lib/components/data-manager/MonthMap.svelte';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import SeriesChart from '$lib/components/data-manager/SeriesChart.svelte';
	import StateChip from '$lib/components/data-manager/StateChip.svelte';
	import { canExport, canExtend, exportSeriesCsv, keyOf, runAction } from '$lib/components/data-manager/actions';
	import {
		exchangeLabel,
		formatBytes,
		formatCount,
		formatDuration,
		formatPercent,
		formatRelative,
		formatUtc,
		historyLength,
		isMarketWide,
		MARKET_WIDE_HELP,
		plural,
		STATE_HELP,
		streamLabel,
		TIER_HELP,
		TIER_LABEL,
		timeframeSeconds,
		VENUE_HELP,
		venueLabel,
		venueShort,
	} from '$lib/components/data-manager/format';
	import { catalogHref, DM, seriesHref } from '$lib/components/data-manager/links';
	import { clock, createRequestGuard, jobsLanded, loading, settle, slaCensus, type Loadable } from '$lib/stores/dataManager';

	const ROWS_PAGE = 50;

	$: symbol = $page.params.symbol ?? '';
	$: timeframe = $page.params.timeframe ?? '';
	$: stream = ($page.url.searchParams.get('stream') ?? 'ohlcv') as DataStream;
	$: venue = $page.url.searchParams.get('venue') ?? 'canonical';
	$: ref = { symbol, timeframe, stream, venue } satisfies SeriesRef;
	$: refKey = `${stream}:${venue}:${symbol}:${timeframe}`;

	let detail: Loadable<SeriesDetail> = loading();
	let notFound = '';
	let loadedKey = '';
	const guard = createRequestGuard();
	const rowsGuard = createRequestGuard();

	let rows: Loadable<RowsResponse> = loading();
	let rowsWindow: { start: string; end: string; label: string } | null = null;
	let rowsOffset = 0;
	let month: string | null = null;
	let focus: { start: string; end: string } | null = null;
	let focusToken = 0;
	let resetToken = 0;
	let reloadToken = 0;
	let chartView = { resolution: '', raw: false, bars: 0, start: 0, end: 0, loading: true, error: '' };
	let sparks: Record<string, number[]> = {};
	let gapsShown = 10;
	let busy = '';
	let deleting = false;

	$: if (refKey !== loadedKey && symbol && timeframe) {
		loadedKey = refKey;
		void loadDetail(false);
	}
	onDestroy(() => {
		guard.cancel();
		rowsGuard.cancel();
	});

	async function loadDetail(keep: boolean) {
		const { signal, current } = guard.next();
		if (!keep) {
			detail = loading();
			rows = loading();
			notFound = '';
			sparks = {};
			month = null;
			rowsWindow = null;
			rowsOffset = 0;
			gapsShown = 10;
			deleting = false;
		}
		try {
			const data = await getSeriesDetail(ref, signal);
			if (!current()) return;
			detail = { status: 'ready', data, error: '', at: Date.now() };
			if (!keep) {
				void loadRows(data);
				void loadSparks(data);
			}
		} catch (error) {
			if (!current()) return;
			if (error instanceof ApiError && error.status === 404 && !isRouteMissingError(error)) {
				notFound = error.message;
				detail = { status: 'error', data: null, error: error.message, at: Date.now() };
			} else {
				detail = await settle(Promise.reject(error), keep ? detail : null);
			}
		}
	}

	$: d = detail.data;
	$: step = timeframeSeconds(timeframe);

	// Work landed (this page's refresh, the collector, another tab): show it
	// without a manual reload. The detail is cheap; the chart, rows and
	// sparklines reload only when this series actually changed.
	let landedSeen = $jobsLanded;
	$: if ($jobsLanded !== landedSeen) {
		landedSeen = $jobsLanded;
		void reloadAfterWork();
	}

	async function reloadAfterWork() {
		const before = d;
		const key = refKey;
		if (!before || detail.status !== 'ready') return;
		await loadDetail(true);
		const after = detail.data;
		if (!after || after === before || key !== refKey) return;
		const moved = (['first_ts', 'last_ts', 'rows', 'gaps_total', 'updated_at'] as const).some((field) => after[field] !== before[field]);
		if (!moved) return;
		reloadToken += 1;
		void loadRows(after);
		void loadSparks(after);
	}

	// Takes the series explicitly: right after a load, `d` has not caught up yet.
	async function loadRows(series: SeriesDetail | null = d) {
		if (!series?.last_ts) {
			rows = { status: 'ready', data: { total: 0, columns: [], rows: [] }, error: '', at: Date.now() };
			return;
		}
		const window = rowsWindow ?? {
			start: new Date(Date.parse(series.last_ts) - (ROWS_PAGE * 2 - 1) * step * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z'),
			end: series.last_ts,
			label: 'latest bars',
		};
		const { signal, current } = rowsGuard.next();
		const order = rowsWindow ? 'asc' : 'desc';
		const next = await settle(getSeriesRows(ref, { start: window.start, end: window.end, limit: ROWS_PAGE, offset: rowsOffset, order }, signal), rows);
		if (current()) rows = next;
	}
	// The latest bars read newest first (what just landed is on top); a month
	// reads in time order. The arrows always move through time.
	$: rowsAtStart = rowsOffset === 0;
	$: rowsAtEnd = !rows.data || rowsOffset + ROWS_PAGE >= rows.data.total;
	$: earlierDisabled = rowsWindow ? rowsAtStart : rowsAtEnd;
	$: laterDisabled = rowsWindow ? rowsAtEnd : rowsAtStart;

	// One symbol can own several series of a stream (OI per timeframe, funding per
	// venue, DVOL per currency), so rows and their sparklines key on all of it.
	const RESTATEMENTS_SHOWN = 12;
	let showAllRestatements = false;

	function streamKey(s: StreamSummary): string {
		return `${s.stream}|${s.venue}|${s.symbol ?? ''}|${s.timeframe}`;
	}

	async function loadSparks(series: SeriesDetail) {
		await Promise.all(
			series.streams.map(async (s) => {
				try {
					let values: number[];
					if (s.stream === 'ohlcv') {
						const bars = await getSeriesBars({ symbol: series.symbol, timeframe: s.timeframe, venue: s.venue }, { max_points: 80 });
						values = bars.bars.map((b) => b.c);
					} else {
						const points = await getStreamPoints(s.symbol ?? series.symbol, s.stream, { timeframe: s.timeframe, venue: s.venue, max_points: 80 });
						const column = points.columns.find((c) => c !== 'timestamp' && c !== 't') ?? '';
						values = points.points.map((p) => Number(p[column]));
					}
					sparks = { ...sparks, [streamKey(s)]: values };
				} catch {
					// A missing sparkline is not worth an error; the row still shows its state.
				}
			}),
		);
	}

	function pickMonth(event: CustomEvent<MonthCell>) {
		const cell = event.detail;
		const [y, m] = cell.month.split('-').map(Number);
		const start = new Date(Date.UTC(y, m - 1, 1)).toISOString().replace(/\.\d{3}Z$/, 'Z');
		const end = new Date(Date.UTC(y, m, 1) - step * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
		month = cell.month;
		focus = { start, end };
		focusToken += 1;
		rowsWindow = { start, end, label: cell.month };
		rowsOffset = 0;
		void loadRows();
	}

	function fullHistory() {
		month = null;
		resetToken += 1;
		rowsWindow = null;
		rowsOffset = 0;
		void loadRows();
	}

	function pageRows(delta: number) {
		rowsOffset = Math.max(0, rowsOffset + (rowsWindow ? delta : -delta) * ROWS_PAGE);
		void loadRows();
	}

	// ---- actions
	async function refresh(mode: 'refresh' | 'repair') {
		busy = mode;
		await runAction(mode === 'repair' ? 'Repairing gaps' : 'Refreshing', () => refreshSeries({ series: [keyOf(d!)], mode }), {
			success: () => `${mode === 'repair' ? 'Repairing gaps in' : 'Refreshing'} ${d!.display_symbol} ${d!.timeframe}. Progress is in Jobs.`,
		});
		busy = '';
	}
	async function extend() {
		busy = 'extend';
		await runAction('Extending history', () => extendHistory({ series: [keyOf(d!)] }), {
			success: () => `Extending ${d!.display_symbol} ${d!.timeframe} from Binance Vision. Progress is in Jobs.`,
		});
		busy = '';
	}
	async function exportCsv() {
		busy = 'export';
		await runAction('Exporting', () => exportSeriesCsv(d!), { success: () => `Exported ${d!.symbol}_${d!.timeframe}.csv`, poke: false });
		busy = '';
	}
	async function setFrozen(frozen: boolean) {
		busy = 'freeze';
		const result = await runAction(frozen ? 'Freezing' : 'Unfreezing', () => freezeSeries({ series: [keyOf(d!)], frozen, reason: frozen ? 'Frozen from the series page' : undefined }), {
			success: () => (frozen ? `Froze ${d!.display_symbol} ${d!.timeframe}. The collector skips it until you unfreeze it.` : `Unfroze ${d!.display_symbol} ${d!.timeframe}.`),
			poke: false,
		});
		busy = '';
		if (result) void loadDetail(true);
	}
	function onDeleted() {
		void goto(catalogHref());
	}

	$: policy = d ? $slaCensus.data?.policy?.[d.sla.tier] ?? null : null;
	$: completeGaps = d?.gaps ?? [];
	$: rowColumns = rows.data?.columns ?? [];
	$: viewText = chartView.raw ? `raw ${timeframe} bars` : chartView.resolution ? `${chartView.resolution} buckets, downsampled from ${timeframe}` : '';
	const cellValue = (value: unknown) =>
		typeof value === 'number' ? (Math.abs(value) >= 1000 ? value.toLocaleString('en-US', { maximumFractionDigits: 4 }) : String(+value.toPrecision(8))) : value == null ? '—' : String(value).replace('T', ' ').replace(/:00Z$/, 'Z');
</script>

<svelte:head><title>{symbol} {timeframe} · Data | Forven</title></svelte:head>

<div class="space-y-3 p-4 pb-24">
	<nav aria-label="Breadcrumb" class="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-[#555]">
		<a href={catalogHref()} class="hover:text-white">Catalog</a>
		<span aria-hidden="true">/</span>
		<a href={catalogHref({ q: symbol })} class="hover:text-white">{symbol}</a>
		<span aria-hidden="true">/</span>
		<span class="text-[#999]">{timeframe} {streamLabel(stream).toLowerCase()}</span>
	</nav>

	{#if notFound}
		<section class="border border-[#333] bg-[#050505] px-5 py-6" role="status">
			<h1 class="text-[15px] font-bold text-white">Nothing is stored for {symbol} {timeframe}{stream === 'ohlcv' ? '' : ` ${streamLabel(stream).toLowerCase()}`}{venue === 'canonical' ? '' : ` on ${venue}`}</h1>
			<p class="mt-1 text-[12px] text-[#888]">{notFound}</p>
			<div class="mt-3 flex gap-2">
				<a href="{DM}/get?symbol={encodeURIComponent(symbol)}" class="terminal-button-primary text-[10px]">Get this data</a>
				<a href={catalogHref({ q: symbol })} class="terminal-button text-[10px]">What is stored for {symbol}</a>
			</div>
		</section>
	{:else if !d}
		<section class="border border-[#222] bg-[#050505]">
			<SectionState state={detail} what="The series" endpoint="GET /api/data/series/{'{symbol}'}/{'{timeframe}'}" rows={6} on:retry={() => loadDetail(false)} />
		</section>
	{:else}
		<!-- Identity, state and actions -->
		<section class="border border-[#222] bg-[#050505] px-4 py-3" aria-labelledby="dm-series-title">
			<div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
				<h1 id="dm-series-title" class="text-[20px] font-bold leading-none text-white">{d.display_symbol}</h1>
				<span class="font-mono text-[15px] text-[#ccc]">{d.timeframe}</span>
				<span class="text-[12px] text-[#888]">{streamLabel(d.stream).toLowerCase()}</span>
				<span class="border px-1.5 py-0.5 text-[10px] {d.venue === 'canonical' ? 'border-[#333] text-[#aaa]' : 'border-sky-900 text-sky-300'}" title={VENUE_HELP}>{venueLabel(d.venue, d.source, d.market)}</span>
				{#if d.venues_available.length > 1}
					<span class="flex items-center gap-1 text-[10px] text-[#666]">also on
						{#each d.venues_available.filter((v) => v !== d?.venue) as other (other)}
							<a href={seriesHref({ symbol: d.symbol, timeframe: d.timeframe, stream: d.stream, venue: other })} class="border border-[#2a2a2a] px-1 text-[#aaa] hover:border-white hover:text-white">{venueShort(other)}</a>
						{/each}
					</span>
				{/if}
				{#if d.asset_class !== 'crypto'}<span class="text-[10px] uppercase tracking-wider text-[#777]">{d.asset_class}</span>{/if}
			</div>
			<div class="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-[#888]">
				<StateChip state={d.sla.state} sla={d.sla} caption />
				<span title={d.quality.issues.join('\n') || 'No issues found'}>Quality <span class="font-mono text-white">{d.quality.score == null ? '—' : Math.round(d.quality.score)}</span>{#if d.quality.issues.length}<span class="text-[#666]">{' '}· {d.quality.issues.length} issue{d.quality.issues.length === 1 ? '' : 's'}</span>{/if}</span>
				{#if isMarketWide(d)}
					<span title={MARKET_WIDE_HELP}>Used <span class="text-white">market-wide</span> <span class="text-[#666]">({TIER_LABEL[d.sla.tier].toLowerCase()})</span></span>
				{:else}
					<span title={TIER_HELP[d.consumers.tier]}>Used by <span class="text-white">{formatCount(d.consumers.count)}</span> <span class="text-[#666]">({TIER_LABEL[d.consumers.tier].toLowerCase()})</span></span>
				{/if}
				<span><span class="font-mono text-white">{formatCount(d.rows)}</span> rows · {historyLength(d.first_ts, d.last_ts)} · {formatBytes(d.size_bytes)}</span>
				{#if d.updated_at}<span title={formatUtc(d.updated_at, { seconds: true })}>written {formatRelative(d.updated_at, $clock)}</span>{/if}
			</div>
			<div class="mt-3 flex flex-wrap gap-1.5">
				<button type="button" class="terminal-button text-[10px]" disabled={!!busy || d.frozen || d.refreshable === false} on:click={() => refresh('refresh')} title={d.frozen ? 'Unfreeze it first' : d.refreshable === false ? (d.refresh_note ?? 'A refresh can’t fetch this series') : 'Bring it current now'}>{busy === 'refresh' ? 'Sending…' : 'Refresh'}</button>
				<button type="button" class="terminal-button text-[10px]" disabled={!!busy || !d.gap_count} on:click={() => refresh('repair')} title={d.gap_count ? `Re-fetch the ${formatCount(d.gap_count)} gaps` : 'No gaps to repair'}>{busy === 'repair' ? 'Sending…' : 'Repair gaps'}</button>
				<button type="button" class="terminal-button text-[10px]" disabled={!!busy || !canExtend(d)} on:click={extend} title={canExtend(d) ? 'Download older history from Binance Vision' : 'Deep history covers research candles, funding, OI and basis'}>{busy === 'extend' ? 'Sending…' : 'Extend history'}</button>
				<button type="button" class="terminal-button text-[10px]" disabled={!!busy || !canExport(d)} on:click={exportCsv} title={canExport(d) ? 'Save every bar as CSV' : 'CSV export covers research candle series'}>{busy === 'export' ? 'Exporting…' : 'Export CSV'}</button>
				<button type="button" class="terminal-button text-[10px]" disabled={!!busy} on:click={() => setFrozen(!d?.frozen)} title={d.frozen ? 'Collect it again' : 'Stop collecting it (history is kept)'}>{d.frozen ? 'Unfreeze' : 'Freeze'}</button>
				<button type="button" class="terminal-button-danger text-[10px]" disabled={!!busy} on:click={() => (deleting = true)}>Delete…</button>
			</div>
			{#if d.frozen}
				<p class="mt-2 text-[11px] text-slate-400">Frozen: {d.frozen_reason ?? 'not collected'}. The history stays; the collector skips it.</p>
			{/if}
		</section>

		{#if deleting}
			<DeleteReview series={[d]} on:cancel={() => (deleting = false)} on:done={onDeleted} />
		{/if}

		<div class="grid gap-3 xl:grid-cols-[minmax(0,1fr)_340px]">
			<div class="min-w-0 space-y-3">
				<!-- Chart -->
				<section class="border border-[#222] bg-[#050505]" aria-label="Chart">
					<header class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[#141414] px-3 py-1.5 text-[10px] text-[#666]">
						<h2 class="text-[11px] font-bold uppercase tracking-wider text-white">{d.stream === 'ohlcv' ? 'Price' : streamLabel(d.stream)}</h2>
						<span>{viewText}{chartView.loading ? ' · loading…' : ''}</span>
						{#if d.stream === 'ohlcv' && d.gaps_total}<span><span class="text-amber-400">▼</span> gap</span>{/if}
						<span class="ml-auto">scroll to zoom · drag to pan</span>
						<button type="button" on:click={fullHistory} disabled={!month && !chartView.raw}
							class="border border-[#2a2a2a] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[#aaa] hover:border-white hover:text-white disabled:opacity-30">Full history</button>
					</header>
					<div class="h-[360px]">
						{#if d.first_ts && d.last_ts}
							{#key refKey}
								<SeriesChart ref={{ symbol: d.symbol, timeframe: d.timeframe, stream: d.stream, venue: d.venue }} first={d.first_ts} last={d.last_ts}
									stepSeconds={step} gaps={d.gaps} {focus} {focusToken} {resetToken} {reloadToken} on:view={(e) => (chartView = e.detail)} />
							{/key}
						{:else}
							<div class="flex h-full items-center justify-center text-[12px] text-[#555]">Nothing stored yet.</div>
						{/if}
					</div>
				</section>

				<!-- Coverage timeline -->
				<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-months">
					<header class="flex items-center gap-2 border-b border-[#141414] px-3 py-1.5">
						<h2 id="dm-months" class="text-[11px] font-bold uppercase tracking-wider text-white">Coverage by month</h2>
						<span class="text-[10px] text-[#666]">
							{#if d.completeness != null}{formatPercent(d.completeness, 2)} of {formatCount(d.expected_rows)} expected bars stored{/if}
							{#if d.synthetic_bars} · {formatCount(d.synthetic_bars)} synthetic{/if}
							{#if d.patched_bars} · {formatCount(d.patched_bars)} from a CSV patch{/if}
						</span>
					</header>
					<MonthMap months={d.month_map} selected={month} on:select={pickMonth} />
				</section>

				<!-- Gaps -->
				<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-gaps">
					<header class="flex items-center gap-2 border-b border-[#141414] px-3 py-1.5">
						<h2 id="dm-gaps" class="text-[11px] font-bold uppercase tracking-wider text-white">Gaps</h2>
						<span class="text-[10px] text-[#666]">{d.gaps_total ? `${formatCount(d.gaps_total)} gap${d.gaps_total === 1 ? '' : 's'}, largest first` : 'none'}</span>
					</header>
					{#if !completeGaps.length}
						<p class="px-3 py-3 text-[11px] text-[#666]">No missing bars between the first and last stored bar.</p>
					{:else}
						<table class="w-full text-[11px]">
							<thead><tr class="text-[9px] uppercase tracking-wider text-[#555]"><th class="px-3 py-1 text-left font-normal">From (UTC)</th><th class="px-2 py-1 text-left font-normal">To (UTC)</th><th class="px-2 py-1 text-right font-normal">Bars</th><th class="px-3 py-1 text-left font-normal">Kind</th></tr></thead>
							<tbody>
								{#each completeGaps.slice(0, gapsShown) as gap (gap.start)}
									<tr class="border-t border-[#111]">
										<td class="px-3 py-1 font-mono text-[10px] text-[#bbb]">{formatUtc(gap.start, { suffix: false })}</td>
										<td class="px-2 py-1 font-mono text-[10px] text-[#bbb]">{formatUtc(gap.end, { suffix: false })}</td>
										<td class="px-2 py-1 text-right font-mono tabular-nums text-white">{formatCount(gap.bars)}</td>
										<td class="px-3 py-1 text-[10px] {gap.kind === 'missing' ? 'text-amber-400' : 'text-[#888]'}"
											title={gap.kind === 'missing' ? 'The venue should have these bars: Repair gaps fetches them' : gap.kind === 'unfillable' ? 'The venue has no bars here (downtime or before listing); not fetched again' : 'Forward-filled bars'}>
											{gap.kind === 'missing' ? 'missing · fetchable' : gap.kind === 'unfillable' ? 'venue has none' : 'synthetic'}</td>
									</tr>
								{/each}
							</tbody>
						</table>
						{#if completeGaps.length > gapsShown}
							<button type="button" on:click={() => (gapsShown += 40)} class="w-full border-t border-[#111] px-3 py-1.5 text-left text-[10px] uppercase tracking-wider text-[#888] hover:text-white">
								Show more ({formatCount(completeGaps.length - gapsShown)} left{d.gaps_total > completeGaps.length ? `, ${formatCount(d.gaps_total - completeGaps.length)} more on the server` : ''})</button>
						{/if}
					{/if}
				</section>

				<!-- Rows -->
				<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-rows">
					<header class="flex flex-wrap items-center gap-2 border-b border-[#141414] px-3 py-1.5">
						<h2 id="dm-rows" class="text-[11px] font-bold uppercase tracking-wider text-white">Stored rows</h2>
						<span class="text-[10px] text-[#666]">{rowsWindow ? `month ${rowsWindow.label}` : 'latest bars, newest first'} · UTC{rows.data ? ` · ${formatCount(rows.data.total)} in the window` : ''}</span>
						<div class="ml-auto flex items-center gap-1">
							<button type="button" on:click={() => pageRows(-1)} disabled={earlierDisabled} aria-label="Earlier page" class="border border-[#2a2a2a] px-2 py-0.5 text-[10px] text-[#aaa] hover:border-white hover:text-white disabled:opacity-30">←</button>
							<span class="font-mono text-[10px] text-[#666]">{rows.data?.total ? `${formatCount(rowsOffset + 1)}–${formatCount(Math.min(rows.data.total, rowsOffset + ROWS_PAGE))}` : ''}</span>
							<button type="button" on:click={() => pageRows(1)} disabled={laterDisabled} aria-label="Later page" class="border border-[#2a2a2a] px-2 py-0.5 text-[10px] text-[#aaa] hover:border-white hover:text-white disabled:opacity-30">→</button>
						</div>
					</header>
					<SectionState state={rows} what="Stored rows" endpoint="GET /api/data/series/…/rows" rows={4} on:retry={() => loadRows()}>
						{#if rows.data?.rows.length}
							<div class="max-h-[340px] overflow-auto">
								<table class="w-full text-[11px]">
									<thead class="sticky top-0 bg-[#0a0a0a]"><tr class="text-[9px] uppercase tracking-wider text-[#555]">{#each rowColumns as column}<th class="px-3 py-1 text-right font-normal first:text-left">{column}</th>{/each}</tr></thead>
									<tbody>
										{#each rows.data.rows as row, i (i)}
											<tr class="border-t border-[#111] hover:bg-white/[0.02]">
												{#each rowColumns as column}<td class="whitespace-nowrap px-3 py-0.5 text-right font-mono text-[10px] tabular-nums text-[#bbb] first:text-left first:text-[#888]">{cellValue(row[column])}</td>{/each}
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{:else}
							<p class="px-3 py-3 text-[11px] text-[#666]">No rows in this window.</p>
						{/if}
					</SectionState>
				</section>
			</div>

			<aside class="min-w-0 space-y-3">
				<!-- Freshness and quality -->
				<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-fresh">
					<header class="border-b border-[#141414] px-3 py-1.5"><h2 id="dm-fresh" class="text-[11px] font-bold uppercase tracking-wider text-white">Freshness & quality</h2></header>
					<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 px-3 py-2 text-[11px]">
						<dt class="text-[#666]">State</dt><dd title={STATE_HELP[d.sla.state]}><StateChip state={d.sla.state} /></dd>
						<dt class="text-[#666]">Tier</dt><dd class="text-[#ccc]" title={TIER_HELP[d.sla.tier]}>{TIER_LABEL[d.sla.tier]}</dd>
						<dt class="text-[#666]">Last bar</dt><dd class="text-[#ccc]">{d.sla.last_bar_ts ? `${formatUtc(d.sla.last_bar_ts)}` : 'none'}{#if d.sla.last_bar_ts}<span class="text-[#666]">{' '}· opened {formatRelative(d.sla.last_bar_ts, $clock)}</span>{/if}</dd>
						<dt class="text-[#666]">Behind</dt><dd class="text-[#ccc]">{formatDuration(d.sla.lag_seconds)} <span class="text-[#666]">of {formatDuration(d.sla.allowed_seconds)} allowed</span></dd>
						{#if policy}<dt class="text-[#666]">Rule</dt><dd class="text-[#888]">{TIER_LABEL[d.sla.tier]} series may miss {policy.missed_bars} bar{policy.missed_bars === 1 ? '' : 's'}, at least {formatDuration(policy.floor_minutes * 60)}; <a href="/settings#data" class="underline hover:text-white">change</a></dd>{/if}
						<dt class="text-[#666]">Quality</dt><dd class="text-[#ccc]">{d.quality.score == null ? 'not scored yet' : `${Math.round(d.quality.score)} / 100`}{#if d.quality.computed_at}<span class="text-[#666]" title={formatUtc(d.quality.computed_at)}>{' '}· scored {formatRelative(d.quality.computed_at, $clock)}</span>{/if}</dd>
						<dt class="text-[#666]">Complete</dt><dd class="text-[#ccc]">{formatPercent(d.completeness, 2)}{#if d.gap_count}<span class="text-[#666]">{' '}· {plural(d.gap_count, 'gap')}, largest {plural(d.largest_gap_bars ?? 0, 'bar')}</span>{/if}</dd>
					</dl>
					{#if d.quality.issues.length}
						<ul class="space-y-0.5 border-t border-[#141414] px-3 py-2 text-[11px] text-amber-400/90">
							{#each d.quality.issues as issue}<li>· {issue}</li>{/each}
						</ul>
					{/if}
				</section>

				<!-- Consumers -->
				<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-consumers">
					<header class="border-b border-[#141414] px-3 py-1.5"><h2 id="dm-consumers" class="text-[11px] font-bold uppercase tracking-wider text-white">Used by</h2></header>
					{#each d.consumers_detail as consumer (consumer.kind + consumer.id)}
						<div class="border-b border-[#111] px-3 py-2 text-[11px] last:border-b-0">
							<div class="flex items-center gap-2">
								<span class="text-[9px] uppercase tracking-wider text-[#666]">{consumer.kind}</span>
								{#if consumer.kind === 'strategy'}
									<a href="/lab/strategy/{encodeURIComponent(consumer.id)}" class="font-mono text-white hover:underline">{consumer.id}</a>
								{:else}<span class="font-mono text-white">{consumer.id}</span>{/if}
								<span class="truncate text-[#aaa]">{consumer.name}</span>
								{#if consumer.stage || consumer.status}<span class="ml-auto shrink-0 border border-[#333] px-1 text-[9px] uppercase tracking-wider text-[#aaa]">{consumer.stage ?? consumer.status}</span>{/if}
							</div>
							{#if consumer.gate}
								<div class="mt-0.5 text-[10px] {consumer.gate.ok ? 'text-emerald-400' : 'text-red-400'}">
									{consumer.gate.ok ? '✓ passes the data gate for its window' : `✗ fails the data gate: ${consumer.gate.reasons.join('; ')}`}
								</div>
							{/if}
						</div>
					{:else}
						<p class="px-3 py-3 text-[11px] text-[#666]">{isMarketWide(d) ? MARKET_WIDE_HELP : `Nothing reads this series${d.sla.tier === 'universe' ? '; it is kept for the research universe' : ''}.`}</p>
					{/each}
				</section>

				<!-- Streams -->
				<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-streams">
					<header class="border-b border-[#141414] px-3 py-1.5"><h2 id="dm-streams" class="text-[11px] font-bold uppercase tracking-wider text-white">{d.stream === 'ohlcv' ? 'Streams for' : 'Other data for'} {d.display_symbol}</h2></header>
					{#each d.streams as s (streamKey(s))}
						<a href={seriesHref({ symbol: s.symbol ?? d.symbol, timeframe: s.timeframe, stream: s.stream, venue: s.venue })}
							class="flex items-center gap-2 border-b border-[#111] px-3 py-1.5 text-[11px] last:border-b-0 hover:bg-white/[0.02]" title={`Columns: ${s.columns.join(', ')}`}>
							<div class="min-w-0 flex-1">
								<div class="flex items-baseline gap-1.5"><span class="text-white">{streamLabel(s.stream)}</span>{#if s.symbol && s.symbol !== d.symbol}<span class="font-mono text-[10px] text-[#aaa]">{s.symbol}</span>{/if}{#if s.venue !== 'canonical'}<span class="text-[10px] text-[#666]">{s.venue}</span>{/if}<span class="font-mono text-[10px] text-[#777]">{s.timeframe}</span></div>
								<div class="text-[10px] text-[#666]">{formatCount(s.rows)} rows · {historyLength(s.first_ts, s.last_ts)}</div>
							</div>
							<MiniLine values={sparks[streamKey(s)] ?? []} label={`${streamLabel(s.stream)} trend`} />
							<StateChip state={s.sla.state} sla={s.sla} />
						</a>
					{:else}
						<p class="px-3 py-3 text-[11px] text-[#666]">No other streams are stored for this symbol.</p>
					{/each}
				</section>

				<!-- Provenance -->
				<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-prov">
					<header class="border-b border-[#141414] px-3 py-1.5"><h2 id="dm-prov" class="text-[11px] font-bold uppercase tracking-wider text-white">Provenance</h2></header>
					<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 px-3 py-2 text-[11px]">
						<dt class="text-[#666]">Source</dt><dd class="text-[#ccc]">{exchangeLabel(d.provenance.source)}{d.provenance.market ? ` · ${d.provenance.market}` : ''}</dd>
						<dt class="text-[#666]">Series</dt><dd class="text-[#ccc]" title={VENUE_HELP}>{venueLabel(d.provenance.venue, d.provenance.source, d.provenance.market)}</dd>
						<dt class="text-[#666]">Stamped as</dt><dd class="font-mono text-[10px] text-[#ccc]">{d.provenance.stamped_symbol ?? 'unstamped'}</dd>
						{#if d.provenance.stamped_at}<dt class="text-[#666]">Stamped</dt><dd class="text-[#ccc]">{formatUtc(d.provenance.stamped_at)}</dd>{/if}
					</dl>
					{#if d.provenance.synthetic_ranges.length || d.provenance.patched_ranges.length || d.provenance.restatements.length}
						<div class="space-y-1 border-t border-[#141414] px-3 py-2 text-[10px]">
							{#each d.provenance.synthetic_ranges as [from, to]}<div class="text-[#aaa]"><span class="text-[#666]">Synthetic</span> {formatUtc(from, { suffix: false })} → {formatUtc(to)}</div>{/each}
							{#each d.provenance.patched_ranges as [from, to]}<div class="text-sky-300"><span class="text-[#666]">CSV patch</span> {formatUtc(from, { suffix: false })} → {formatUtc(to)}</div>{/each}
							{#each showAllRestatements ? d.provenance.restatements : d.provenance.restatements.slice(0, RESTATEMENTS_SHOWN) as r}
								<div class="text-amber-400/90"><span class="text-[#666]">Restated</span> {formatCount(r.rows)} rows {r.first_ts ? `${formatUtc(r.first_ts, { suffix: false })} → ${formatUtc(r.last_ts)}` : ''} <span class="text-[#666]">(seen {formatUtc(r.observed_at, { date: true })})</span></div>
							{/each}
							{#if d.provenance.restatements.length > RESTATEMENTS_SHOWN}
								<button type="button" class="text-[10px] text-[#888] underline hover:text-white" on:click={() => (showAllRestatements = !showAllRestatements)}>
									{showAllRestatements ? 'Show fewer' : `Show all ${d.provenance.restatements.length} restatements`}
								</button>
							{/if}
						</div>
					{:else}
						<p class="border-t border-[#141414] px-3 py-2 text-[10px] text-[#666]">No synthetic, patched or restated bars.</p>
					{/if}
				</section>

				<!-- Recent jobs -->
				<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-series-jobs">
					<header class="flex items-center border-b border-[#141414] px-3 py-1.5"><h2 id="dm-series-jobs" class="text-[11px] font-bold uppercase tracking-wider text-white">Recent jobs</h2>
						<a href="{DM}/jobs" class="ml-auto text-[10px] text-[#888] hover:text-white">All →</a></header>
					{#each d.recent_jobs as job (job.id)}
						<JobRow {job} on:changed={() => loadDetail(true)} />
					{:else}
						<p class="px-3 py-3 text-[11px] text-[#666]">No jobs have touched this series recently.</p>
					{/each}
				</section>
			</aside>
		</div>
	{/if}
</div>
