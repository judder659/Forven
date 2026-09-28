<script lang="ts">
	// Coverage: where are the holes? One stream at a time, symbols × timeframes,
	// grouped by tier, with the research plan's missing series outlined.
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { estimateDownloads, getCatalog, getUniversePlanDiff, refreshSeries, startDownloads } from '$lib/api/dataManager';
	import type { CatalogRow, DataStream, DownloadEstimateResponse, DownloadRequestItem, UniversePlanDiff } from '$lib/api/dataManagerTypes';
	import CoverageGrid from '$lib/components/data-manager/CoverageGrid.svelte';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import { keyOf, runAction } from '$lib/components/data-manager/actions';
	import { buildCoverage, type CoverageCell } from '$lib/components/data-manager/coverage';
	import { formatBytes, formatCount, formatDuration, plural, STATE_HELP, STATE_LABEL, stateFillClass, STREAM_LABEL, STREAMS, venueShort } from '$lib/components/data-manager/format';
	import { seriesHref } from '$lib/components/data-manager/links';
	import { createRequestGuard, loading, pageSearch, settle, type Loadable } from '$lib/stores/dataManager';

	const PAGE = 500;
	const LEGEND_STATES = ['fresh', 'late', 'breach', 'frozen', 'missing'] as const;
	let stream: DataStream = 'ohlcv';
	let venue = 'canonical';
	let q = '';
	let showFrozen = false;
	let mode: 'freshness' | 'depth' = 'freshness';
	let searchInput: HTMLInputElement | undefined;

	let rows: Loadable<CatalogRow[]> = loading();
	let streamCounts: Record<string, number> = {};
	let plan: Loadable<UniversePlanDiff> = loading();
	let selected = new Set<string>();
	const guard = createRequestGuard();

	async function load() {
		const { signal, current } = guard.next();
		rows = loading();
		selected = new Set();
		try {
			const all: CatalogRow[] = [];
			for (let offset = 0; ; offset += PAGE) {
				const page = await getCatalog({ stream: [stream], sort: 'symbol', order: 'asc', limit: PAGE, offset }, signal);
				if (!current()) return;
				all.push(...page.rows);
				streamCounts = page.facets.stream;
				if (all.length >= page.total || page.rows.length < PAGE) break;
			}
			rows = { status: 'ready', data: all, error: '', at: Date.now() };
			const venues = new Set(all.map((r) => r.venue));
			if (!venues.has(venue)) venue = venues.has('canonical') ? 'canonical' : [...venues][0] ?? 'canonical';
		} catch (error) {
			if (!current()) return;
			rows = await settle<CatalogRow[]>(Promise.reject(error));
		}
	}
	const loadPlan = async () => (plan = await settle(getUniversePlanDiff(), plan));

	onMount(() => {
		pageSearch.set(searchInput ?? null);
		void load();
		void loadPlan();
	});
	onDestroy(() => {
		pageSearch.set(null);
		guard.cancel();
	});

	function chooseStream(next: DataStream) {
		if (next === stream) return;
		stream = next;
		void load();
	}

	$: venues = [...new Set((rows.data ?? []).map((r) => r.venue))].sort((a, b) => (a === 'canonical' ? -1 : b === 'canonical' ? 1 : a.localeCompare(b)));
	$: model = buildCoverage(rows.data ?? [], plan.data, { stream, venue, q, showFrozen });
	$: cells = model.flat.flat();
	$: chosen = cells.filter((cell) => selected.has(cell.key));
	$: storedChosen = chosen.filter((cell) => cell.row && cell.row.rows > 0);
	$: missingChosen = chosen.filter((cell) => !cell.row || cell.row.rows === 0);
	$: symbolCount = model.groups.reduce((sum, g) => sum + g.symbols.length, 0);
	$: plannedMissing = cells.filter((c) => c.planned).length;

	function open(event: CustomEvent<CoverageCell>) {
		const row = event.detail.row;
		if (row) void goto(seriesHref(row));
	}

	let refreshing = false;
	async function refreshSelected() {
		refreshing = true;
		const series = storedChosen.map((c) => keyOf(c.row!));
		await runAction('Refreshing', () => refreshSeries({ series, mode: 'refresh' }), {
			success: () => `Refreshing ${plural(series.length, 'series', 'series')}. Progress is in Jobs.`,
		});
		refreshing = false;
	}

	// Download missing: estimate first, then start.
	let downloadItems: DownloadRequestItem[] = [];
	let estimate: Loadable<DownloadEstimateResponse> | null = null;
	let starting = false;
	async function reviewDownload() {
		downloadItems = missingChosen
			.filter(() => stream === 'ohlcv')
			.map((cell) => ({ symbol: cell.symbol, timeframe: cell.timeframe, venue: venue === 'canonical' ? 'canonical' : venue, history: { mode: 'all' as const } }));
		estimate = loading();
		estimate = await settle(estimateDownloads(downloadItems));
	}
	async function startDownload() {
		starting = true;
		const result = await runAction('Starting downloads', () => startDownloads(downloadItems), {
			success: (r) => `Started ${plural(r.jobs.length, 'download')}. You can leave this page; progress is in Jobs.`,
		});
		starting = false;
		if (result) {
			estimate = null;
			selected = new Set();
		}
	}
</script>

<svelte:head><title>Data · Coverage | Forven</title></svelte:head>

<div class="flex h-full min-h-0 flex-col gap-2 p-4 pb-3">
	<div class="flex flex-wrap items-center gap-2">
		<div class="inline-flex flex-wrap border border-[#2a2a2a]" role="group" aria-label="Stream">
			{#each STREAMS as s (s)}
				<button type="button" on:click={() => chooseStream(s)} aria-pressed={stream === s}
					class="border-r border-[#2a2a2a] px-2.5 py-1 text-[10px] uppercase tracking-wider last:border-r-0 transition-colors {stream === s ? 'bg-white text-black' : 'text-[#777] hover:text-white'}">
					{STREAM_LABEL[s]}{#if streamCounts[s] != null}<span class="ml-1 font-mono normal-case {stream === s ? 'text-[#444]' : 'text-[#555]'}">{formatCount(streamCounts[s])}</span>{/if}
				</button>
			{/each}
		</div>
		{#if venues.length > 1}
			<label class="flex items-center gap-1 text-[10px] uppercase tracking-wider text-[#666]">Venue
				<select bind:value={venue} class="border border-[#2a2a2a] bg-black px-1.5 py-1 text-[10px] normal-case text-[#ccc] outline-none focus:border-white">
					{#each venues as v}<option value={v}>{v === 'canonical' ? 'Research series' : venueShort(v)}</option>{/each}
				</select>
			</label>
		{/if}
		<input bind:this={searchInput} bind:value={q} type="search" placeholder="Filter symbols" aria-label="Filter symbols" spellcheck="false"
			class="w-40 border border-[#2a2a2a] bg-black px-2 py-1 font-mono text-[11px] text-white outline-none placeholder:text-[#555] focus:border-white" />
		<label class="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-[#777]">
			<input type="checkbox" bind:checked={showFrozen} class="accent-white" /> Show frozen
		</label>
		<div class="ml-auto inline-flex border border-[#2a2a2a]" role="group" aria-label="Colour by">
			<button type="button" on:click={() => (mode = 'freshness')} aria-pressed={mode === 'freshness'} class="px-2.5 py-1 text-[10px] uppercase tracking-wider {mode === 'freshness' ? 'bg-white text-black' : 'text-[#777] hover:text-white'}">Freshness</button>
			<button type="button" on:click={() => (mode = 'depth')} aria-pressed={mode === 'depth'} class="border-l border-[#2a2a2a] px-2.5 py-1 text-[10px] uppercase tracking-wider {mode === 'depth' ? 'bg-white text-black' : 'text-[#777] hover:text-white'}">History depth</button>
		</div>
	</div>

	<div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-[#777]" aria-label="Legend">
		{#if mode === 'freshness'}
			{#each LEGEND_STATES as state}
				<span class="flex items-center gap-1" title={STATE_HELP[state]}><span class="h-2.5 w-3.5 {stateFillClass(state)}"></span>{STATE_LABEL[state]}{state === 'late' ? ' (!)' : state === 'breach' ? ' (‼)' : state === 'frozen' ? ' (*)' : ''}</span>
			{/each}
		{:else}
			<span class="flex items-center gap-1"><span class="h-2.5 w-10 bg-gradient-to-r from-[rgba(147,197,253,0.1)] to-[rgba(147,197,253,0.63)]"></span>less → more history (log scale)</span>
		{/if}
		<span class="flex items-center gap-1"><span class="h-2.5 w-3.5 border border-dashed border-sky-800"></span>planned, not stored</span>
		<span class="ml-auto">
			{formatCount(symbolCount)} symbols · {formatCount(model.timeframes.length)} timeframes{#if plannedMissing} · <span class="text-sky-300/80">{formatCount(plannedMissing)} planned missing</span>{/if}
			· label = history stored · drag or shift-click to select
		</span>
	</div>

	{#if selected.size}
		<div class="flex flex-wrap items-center gap-2 border border-[#333] bg-[#0a0a0a] px-3 py-1.5" role="toolbar" aria-label="Actions on the selected cells">
			<span class="text-[11px] text-white"><span class="font-mono">{formatCount(selected.size)}</span> cells: {formatCount(storedChosen.length)} stored · {formatCount(missingChosen.length)} not stored</span>
			<button type="button" class="terminal-button text-[10px]" disabled={!storedChosen.length || refreshing} on:click={refreshSelected}>{refreshing ? 'Sending…' : `Refresh selected (${storedChosen.length})`}</button>
			<button type="button" class="terminal-button text-[10px]" disabled={!missingChosen.length || stream !== 'ohlcv'} on:click={reviewDownload}
				title={stream === 'ohlcv' ? 'Estimate, then download all history for the cells with nothing stored' : 'Streams are collected with their candles; download the candles instead'}>Download missing ({missingChosen.length})</button>
			<button type="button" on:click={() => { selected = new Set(); estimate = null; }} class="ml-auto text-[10px] uppercase tracking-wider text-[#777] hover:text-white">Clear</button>
		</div>
		{#if estimate}
			<div class="border border-[#333] bg-[#050505] px-3 py-2 text-[11px]" aria-live="polite">
				{#if estimate.status === 'loading'}
					<span class="text-[#888]">Estimating {plural(downloadItems.length, 'download')}…</span>
				{:else if estimate.status === 'ready' && estimate.data}
					{@const e = estimate.data}
					{@const blocked = e.estimates.filter((x) => x.blocked)}
					<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
						<span class="text-white">{plural(downloadItems.length - blocked.length, 'series', 'series')} · about {formatBytes(e.total_bytes)} · {formatDuration(e.total_seconds)}</span>
						<span class="text-[#777]">{formatBytes(e.disk_free_bytes)} free on disk</span>
						<button type="button" class="terminal-button-primary ml-auto text-[10px]" disabled={starting || downloadItems.length === blocked.length} on:click={startDownload}>{starting ? 'Starting…' : 'Start downloads'}</button>
						<button type="button" class="terminal-button text-[10px]" on:click={() => (estimate = null)}>Cancel</button>
					</div>
					{#each [...e.warnings, ...e.estimates.flatMap((x) => x.warnings)] as warning}<div class="mt-1 text-amber-400">{warning}</div>{/each}
					{#each blocked as b}<div class="mt-1 text-red-400">{b.item.symbol} {b.item.timeframe}: {b.blocked}</div>{/each}
				{:else}
					<SectionState state={estimate} what="The download estimate" endpoint="POST /api/data/acquire/estimate" on:retry={reviewDownload} />
				{/if}
			</div>
		{/if}
	{/if}

	{#if rows.status === 'ready'}
		{#if !model.groups.length}
			<div class="flex flex-1 flex-col items-center justify-center border border-dashed border-[#262626] px-6 py-12 text-center">
				<p class="text-[13px] text-white">{q ? `No symbol matches “${q}”.` : `Nothing is stored for ${STREAM_LABEL[stream].toLowerCase()} yet.`}</p>
				<p class="mt-1 text-[11px] text-[#777]">{q ? 'Clear the filter to see every symbol.' : 'Perp streams are collected alongside their candles. Get data adds them.'}</p>
			</div>
		{:else}
			<CoverageGrid {model} {mode} {selected} on:open={open} on:select={(e) => (selected = new Set(e.detail))} />
		{/if}
		{#if plan.status === 'unavailable' && stream === 'ohlcv'}
			<p class="text-[10px] text-[#555]">Planned-but-missing research cells need GET /api/data/universe/plan-diff, which this backend does not serve yet.</p>
		{/if}
	{:else}
		<div class="border border-[#222] bg-[#050505]">
			<SectionState state={rows} what="The coverage grid" endpoint="GET /api/data/catalog" rows={10} on:retry={load} />
		</div>
	{/if}
</div>
