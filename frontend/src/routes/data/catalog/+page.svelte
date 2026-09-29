<script lang="ts">
	// Catalog: every stored series with its freshness, completeness, quality and
	// consumers. Filters live in the URL (so views and Health links are
	// shareable); filtering and sorting happen on the server.
	import { onDestroy, onMount, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { isRouteMissingError } from '$lib/api/core';
	import { extendHistory, freezeSeries, getCatalog, refreshSeries } from '$lib/api/dataManager';
	import type { CatalogFacets, CatalogRow } from '$lib/api/dataManagerTypes';
	import CatalogTable from '$lib/components/data-manager/CatalogTable.svelte';
	import ChoiceMenu from '$lib/components/data-manager/ChoiceMenu.svelte';
	import DeleteReview from '$lib/components/data-manager/DeleteReview.svelte';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import { canExport, canExtend, exportSeriesCsv, keyOf, runAction } from '$lib/components/data-manager/actions';
	import {
		activeFilterCount,
		applyView,
		COLUMNS,
		defaultOrder,
		EMPTY_FILTERS,
		filtersFromParams,
		filtersToParams,
		filtersToQuery,
		loadColumns,
		loadViews,
		saveColumns,
		saveCustomViews,
		SORT_LABEL,
		SORTS,
		viewFromFilters,
		viewMatches,
		type CatalogFilters,
		type ColumnKey,
		type FacetKey,
		type SavedView,
		type SortKey,
	} from '$lib/components/data-manager/catalogViews';
	import { compareTimeframes } from '$lib/components/data-manager/coverage';
	import {
		formatCount,
		formatRelative,
		plural,
		STATE_HELP,
		STATE_LABEL,
		STATES,
		STREAM_LABEL,
		STREAMS,
		TIER_HELP,
		TIER_LABEL,
		TIERS,
		venueLabel,
		venueShort,
	} from '$lib/components/data-manager/format';
	import { DM, seriesHref } from '$lib/components/data-manager/links';
	import { clock, createRequestGuard, errorMessage, jobsLanded, loading, pageSearch, type Loadable } from '$lib/stores/dataManager';

	const PAGE = 500;
	const MAX_ROWS = 5000;
	const FACET_LABEL: Record<FacetKey, string> = { tier: 'Tier', state: 'State', stream: 'Stream', timeframe: 'Timeframe', venue: 'Venue', asset_class: 'Asset class' };

	let filters: CatalogFilters = filtersFromParams($page.url.searchParams);
	let lastSearch = $page.url.search;
	let searchText = filters.q;
	let searchInput: HTMLInputElement | undefined;
	let searchTimer: ReturnType<typeof setTimeout> | undefined;

	let state: Loadable<true> = loading();
	let rows: CatalogRow[] = [];
	let total = 0;
	let fullTotal = 0;
	let facets: CatalogFacets | null = null;
	let generatedAt: string | null = null;
	let loadingMore = false;
	let resetToken = 0;
	const guard = createRequestGuard();

	let views: SavedView[] = loadViews();
	let columns: ColumnKey[] = loadColumns();
	let savingView = false;
	let viewName = '';

	let selected = new Map<string, CatalogRow>();
	let anchor = -1;
	let deleting = false;
	let busy = '';

	// A link (tier card, back/forward) changed the URL: follow it.
	$: if ($page.url.search !== lastSearch) {
		lastSearch = $page.url.search;
		filters = filtersFromParams($page.url.searchParams);
		searchText = filters.q;
		void load();
	}

	function setFilters(next: CatalogFilters) {
		filters = next;
		const params = filtersToParams(next).toString();
		lastSearch = params ? `?${params}` : '';
		void goto(`${DM}/catalog${lastSearch}`, { replaceState: true, keepFocus: true, noScroll: true });
		void load();
	}

	async function load(options: { keepPosition?: boolean } = {}) {
		const { signal, current } = guard.next();
		loadingMore = true;
		try {
			const first = await getCatalog(filtersToQuery(filters, { limit: PAGE, offset: 0 }), signal);
			if (!current()) return;
			rows = first.rows;
			fullTotal = first.total;
			total = Math.min(first.total, MAX_ROWS);
			facets = first.facets;
			generatedAt = first.generated_at;
			state = { status: 'ready', data: true, error: '', at: Date.now() };
			if (!options.keepPosition) resetToken += 1;
			for (let offset = PAGE; offset < total; offset += PAGE) {
				const next = await getCatalog(filtersToQuery(filters, { limit: PAGE, offset }), signal);
				if (!current()) return;
				rows = [...rows, ...next.rows];
				if (next.rows.length < PAGE) {
					total = Math.min(total, rows.length);
					break;
				}
			}
		} catch (error) {
			if (!current()) return;
			if (isRouteMissingError(error)) state = { status: 'unavailable', data: null, error: '', at: Date.now() };
			else if (rows.length) state = { ...state, error: errorMessage(error) };
			else state = { status: 'error', data: null, error: errorMessage(error), at: Date.now() };
		} finally {
			if (current()) loadingMore = false;
		}
	}

	onMount(() => {
		pageSearch.set(searchInput ?? null);
		void load();
	});

	// Work landed: reload in place (same scroll, same selection).
	let landedSeen = $jobsLanded;
	$: if ($jobsLanded !== landedSeen) {
		landedSeen = $jobsLanded;
		void load({ keepPosition: true });
	}
	onDestroy(() => {
		pageSearch.set(null);
		guard.cancel();
		clearTimeout(searchTimer);
	});

	function onSearch() {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => setFilters({ ...filters, q: searchText }), 250);
	}
	function onSearchKey(event: KeyboardEvent) {
		if (event.key === 'Escape' && searchText) {
			event.preventDefault();
			searchText = '';
			setFilters({ ...filters, q: '' });
		} else if (event.key === 'ArrowDown' || event.key === 'Enter') {
			event.preventDefault();
			clearTimeout(searchTimer);
			if (searchText !== filters.q) setFilters({ ...filters, q: searchText });
			document.querySelector<HTMLElement>('[data-testid="catalog-grid"]')?.focus();
		}
	}

	function setSort(sort: SortKey) {
		const order = filters.sort === sort ? (filters.order === 'asc' ? 'desc' : 'asc') : defaultOrder(sort);
		setFilters({ ...filters, sort, order });
	}

	// ---- facets
	const TF_LIST = (counts: Record<string, number>) => Object.keys(counts).sort(compareTimeframes);
	function facetItems(key: FacetKey, counts: Record<string, number> = {}) {
		const ordered =
			key === 'tier' ? TIERS.filter((t) => counts[t] != null)
			: key === 'state' ? STATES.filter((s) => counts[s] != null)
			: key === 'stream' ? STREAMS.filter((s) => counts[s] != null)
			: key === 'timeframe' ? TF_LIST(counts)
			: Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
		return ordered.map((value) => ({
			value,
			count: counts[value] ?? 0,
			label:
				key === 'tier' ? TIER_LABEL[value as keyof typeof TIER_LABEL]
				: key === 'state' ? STATE_LABEL[value as keyof typeof STATE_LABEL]
				: key === 'stream' ? STREAM_LABEL[value as keyof typeof STREAM_LABEL]
				: key === 'venue' ? venueShort(value)
				: key === 'asset_class' ? value.charAt(0).toUpperCase() + value.slice(1)
				: value,
			title:
				key === 'tier' ? TIER_HELP[value as keyof typeof TIER_HELP]
				: key === 'state' ? STATE_HELP[value as keyof typeof STATE_HELP]
				: key === 'venue' ? venueLabel(value)
				: undefined,
		}));
	}

	// ---- views and columns
	function chooseView(view: SavedView) {
		searchText = view.filters.q ?? '';
		setFilters(applyView(view));
	}
	async function startSaveView() {
		savingView = true;
		viewName = '';
		await tick();
		document.getElementById('dm-view-name')?.focus();
	}
	function saveView() {
		if (!viewName.trim()) return;
		views = [...views, viewFromFilters(viewName, filters)];
		saveCustomViews(views);
		savingView = false;
	}
	function removeView(view: SavedView) {
		views = views.filter((v) => v.id !== view.id);
		saveCustomViews(views);
	}
	function setColumns(next: string[]) {
		columns = COLUMNS.map((c) => c.key).filter((key) => next.includes(key));
		saveColumns(columns);
	}

	// ---- selection
	$: selectedIds = new Set(selected.keys());
	$: chosen = [...selected.values()];
	$: hiddenSelected = chosen.filter((row) => !rows.some((r) => r.id === row.id)).length;
	function toggle(event: CustomEvent<{ row: CatalogRow; index: number; shift: boolean }>) {
		const { row, index, shift } = event.detail;
		const next = new Map(selected);
		if (shift && anchor >= 0 && anchor !== index) {
			const [from, to] = [Math.min(anchor, index), Math.max(anchor, index)];
			for (const r of rows.slice(from, to + 1)) next.set(r.id, r);
		} else if (next.has(row.id)) next.delete(row.id);
		else next.set(row.id, row);
		anchor = index;
		selected = next;
	}
	function selectAll() {
		selected = new Map([...selected, ...rows.map((r) => [r.id, r] as const)]);
	}
	function clearSelection() {
		selected = new Map();
		anchor = -1;
		deleting = false;
	}

	// ---- bulk actions
	$: extendable = chosen.filter(canExtend);
	$: exportable = chosen.filter(canExport);
	async function bulkRefresh(mode: 'refresh' | 'repair') {
		busy = mode;
		const count = chosen.length;
		await runAction(mode === 'repair' ? 'Repairing gaps' : 'Refreshing', () => refreshSeries({ series: chosen.map(keyOf), mode }), {
			success: () => `${mode === 'repair' ? 'Repairing gaps in' : 'Refreshing'} ${plural(count, 'series', 'series')}. Progress is in Jobs.`,
		});
		busy = '';
	}
	async function bulkExtend() {
		busy = 'extend';
		const count = extendable.length;
		await runAction('Extending history', () => extendHistory({ series: extendable.map(keyOf) }), {
			success: () => `Extending history for ${plural(count, 'series', 'series')} from Binance Vision. Progress is in Jobs.`,
		});
		busy = '';
	}
	async function bulkExport() {
		busy = 'export';
		await runAction('Exporting', async () => {
			for (const row of exportable.slice(0, 10)) await exportSeriesCsv(row);
			return exportable.length;
		}, { success: (n) => `Exported ${plural(Math.min(n, 10), 'file', 'files')}.`, poke: false });
		busy = '';
	}
	async function bulkFreeze(frozen: boolean) {
		busy = frozen ? 'freeze' : 'unfreeze';
		const targets = chosen.filter((row) => row.frozen !== frozen);
		const result = await runAction(frozen ? 'Freezing' : 'Unfreezing', () => freezeSeries({ series: targets.map(keyOf), frozen, reason: frozen ? 'Frozen from the catalog' : undefined }), {
			success: (r) => `${frozen ? 'Froze' : 'Unfroze'} ${plural(r.updated, 'series', 'series')}.${frozen ? ' The collector skips them until you unfreeze.' : ''}`,
			poke: false,
		});
		busy = '';
		if (result) {
			clearSelection();
			void load({ keepPosition: true });
		}
	}
	function onDeleted() {
		clearSelection();
		void load({ keepPosition: true });
	}
	function openRow(event: CustomEvent<CatalogRow>) {
		void goto(seriesHref(event.detail));
	}

	$: filterCount = activeFilterCount(filters);
</script>

<svelte:head><title>Data · Catalog | Forven</title></svelte:head>

<div class="flex h-full min-h-0 flex-col gap-2 p-4 pb-3">
	<!-- Search, views, sort and columns -->
	<div class="flex flex-wrap items-center gap-2">
		<div class="relative">
			<input bind:this={searchInput} bind:value={searchText} on:input={onSearch} on:keydown={onSearchKey} type="search"
				placeholder="Symbol or alias (BTC, btcusdt, BTC/USDT)" aria-label="Search the catalog" spellcheck="false"
				class="w-72 border border-[#2a2a2a] bg-black py-1 pl-2 pr-7 font-mono text-[12px] text-white outline-none placeholder:text-[#555] focus:border-white" />
			<kbd class="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 border border-[#333] px-1 text-[9px] text-[#666]" aria-hidden="true">/</kbd>
		</div>
		<div class="flex flex-wrap items-center gap-1" role="group" aria-label="Saved views">
			{#each views as view (view.id)}
				{@const on = viewMatches(view, filters)}
				<span class="group relative inline-flex">
					<button type="button" on:click={() => chooseView(view)} aria-pressed={on}
						class="border px-2 py-1 text-[10px] uppercase tracking-wider transition-colors {on ? 'border-white bg-white text-black' : 'border-[#2a2a2a] text-[#888] hover:border-[#555] hover:text-white'}">{view.name}</button>
					{#if !view.builtin}
						<button type="button" on:click={() => removeView(view)} aria-label={`Delete view ${view.name}`}
							class="-ml-px border border-[#2a2a2a] px-1 text-[10px] text-[#555] hover:text-red-400">✕</button>
					{/if}
				</span>
			{/each}
			{#if savingView}
				<form on:submit|preventDefault={saveView} class="inline-flex">
					<input id="dm-view-name" bind:value={viewName} placeholder="View name" aria-label="View name"
						on:keydown={(e) => e.key === 'Escape' && (savingView = false)}
						class="w-32 border border-white bg-black px-2 py-0.5 text-[11px] text-white outline-none" />
					<button type="submit" disabled={!viewName.trim()} class="-ml-px border border-white bg-white px-2 text-[10px] uppercase text-black disabled:opacity-40">Save</button>
				</form>
			{:else}
				<button type="button" on:click={startSaveView} title="Save the current filters, search and sort as a view"
					class="border border-dashed border-[#333] px-2 py-1 text-[10px] uppercase tracking-wider text-[#666] hover:border-[#666] hover:text-white">+ Save view</button>
			{/if}
		</div>
		<div class="ml-auto flex items-center gap-1.5">
			<label class="sr-only" for="dm-sort">Sort by</label>
			<select id="dm-sort" value={filters.sort} on:change={(e) => setFilters({ ...filters, sort: e.currentTarget.value as SortKey, order: defaultOrder(e.currentTarget.value as SortKey) })}
				class="border border-[#2a2a2a] bg-black px-1.5 py-1 text-[10px] uppercase tracking-wider text-[#ccc] outline-none focus:border-white">
				{#each SORTS as sort}<option value={sort}>{SORT_LABEL[sort]}</option>{/each}
			</select>
			<button type="button" on:click={() => setFilters({ ...filters, order: filters.order === 'asc' ? 'desc' : 'asc' })}
				aria-label={filters.order === 'asc' ? 'Ascending; switch to descending' : 'Descending; switch to ascending'}
				class="border border-[#2a2a2a] px-2 py-1 text-[11px] text-[#aaa] hover:border-white hover:text-white">{filters.order === 'asc' ? '↑' : '↓'}</button>
			<ChoiceMenu label="Columns" clearable={false} summary=""
				items={COLUMNS.map((c) => ({ value: c.key, label: c.label, title: c.title }))} selected={columns}
				on:change={(e) => setColumns(e.detail)} />
		</div>
	</div>

	<!-- Filters -->
	<div class="flex flex-wrap items-center gap-1.5">
		{#each ['tier', 'state', 'stream', 'timeframe', 'venue', 'asset_class'] as key (key)}
			{@const facet = key as FacetKey}
			<ChoiceMenu label={FACET_LABEL[facet]} items={facetItems(facet, facets?.[facet])} selected={filters[facet]}
				on:change={(e) => setFilters({ ...filters, [facet]: e.detail })} />
		{/each}
		{#if filterCount}
			<button type="button" on:click={() => { searchText = ''; setFilters({ ...EMPTY_FILTERS, sort: filters.sort, order: filters.order }); }}
				class="px-1.5 text-[10px] uppercase tracking-wider text-[#777] hover:text-white">Clear filters</button>
		{/if}
		<span class="ml-auto text-[10px] text-[#666]" aria-live="polite">
			{#if state.status === 'ready'}
				<span class="font-mono tabular-nums text-[#aaa]">{formatCount(fullTotal)}</span> {fullTotal === 1 ? 'series' : 'series'}
				{#if loadingMore}· loading {formatCount(rows.length)} of {formatCount(total)}…{/if}
				{#if fullTotal > MAX_ROWS}· showing the first {formatCount(MAX_ROWS)}; narrow the filters{/if}
				{#if generatedAt}· as of {formatRelative(generatedAt, $clock)}{/if}
			{/if}
		</span>
	</div>

	<!-- Bulk actions -->
	{#if selected.size}
		<div class="flex shrink-0 flex-wrap items-center gap-1.5 border border-[#333] bg-[#0a0a0a] px-3 py-1.5" role="toolbar" aria-label="Actions on the selected series" data-testid="bulk-bar">
			<span class="mr-1 text-[11px] text-white"><span class="font-mono tabular-nums">{formatCount(selected.size)}</span> selected{#if hiddenSelected}<span class="text-[#777]">{' '}({hiddenSelected} hidden by the filters)</span>{/if}</span>
			<button type="button" class="terminal-button text-[10px]" disabled={!!busy} on:click={() => bulkRefresh('refresh')} title="Bring them current now">{busy === 'refresh' ? 'Sending…' : 'Refresh'}</button>
			<button type="button" class="terminal-button text-[10px]" disabled={!!busy} on:click={() => bulkRefresh('repair')} title="Re-fetch the missing bars inside their history">{busy === 'repair' ? 'Sending…' : 'Repair gaps'}</button>
			<button type="button" class="terminal-button text-[10px]" disabled={!!busy || !extendable.length} on:click={bulkExtend}
				title={extendable.length ? `Download older history from Binance Vision for ${extendable.length} of them` : 'Deep history covers research candles, funding, OI and basis only'}>{busy === 'extend' ? 'Sending…' : 'Extend history'}</button>
			<button type="button" class="terminal-button text-[10px]" disabled={!!busy || !exportable.length} on:click={bulkExport}
				title={exportable.length ? `CSV of ${Math.min(10, exportable.length)} research candle series${exportable.length > 10 ? ' (first 10)' : ''}` : 'CSV export covers research candle series'}>{busy === 'export' ? 'Exporting…' : 'Export CSV'}</button>
			{#if chosen.some((r) => !r.frozen)}
				<button type="button" class="terminal-button text-[10px]" disabled={!!busy} on:click={() => bulkFreeze(true)} title="Stop collecting them (history is kept)">Freeze</button>
			{/if}
			{#if chosen.some((r) => r.frozen)}
				<button type="button" class="terminal-button text-[10px]" disabled={!!busy} on:click={() => bulkFreeze(false)} title="Collect them again">Unfreeze</button>
			{/if}
			<button type="button" class="terminal-button-danger text-[10px]" disabled={!!busy} on:click={() => (deleting = true)}>Delete…</button>
			<button type="button" on:click={clearSelection} class="ml-auto text-[10px] uppercase tracking-wider text-[#777] hover:text-white">Clear selection</button>
		</div>
		{#if deleting}
			<div class="shrink-0"><DeleteReview series={chosen} on:cancel={() => (deleting = false)} on:done={onDeleted} /></div>
		{/if}
	{/if}

	{#if state.status === 'ready' && total === 0}
		<div class="flex flex-1 flex-col items-center justify-center border border-dashed border-[#262626] px-6 py-12 text-center">
			{#if filterCount}
				<p class="text-[13px] text-white">No series match these filters.</p>
				<p class="mt-1 text-[11px] text-[#777]">Try another view, or clear the filters to see everything that is stored.</p>
				<button type="button" on:click={() => { searchText = ''; setFilters({ ...EMPTY_FILTERS }); }} class="terminal-button mt-3 text-[10px]">Clear filters</button>
			{:else}
				<p class="text-[13px] text-white">No market data is stored yet.</p>
				<p class="mt-1 max-w-md text-[11px] leading-relaxed text-[#777]">Every backtest, the gauntlet and paper trading read stored series. Start with a preset, or pick exactly the markets you want.</p>
				<div class="mt-3 flex gap-2">
					<a href="{DM}/setup" class="terminal-button-primary text-[10px]">Set up data</a>
					<a href="{DM}/get" class="terminal-button text-[10px]">Get data</a>
				</div>
			{/if}
		</div>
	{:else if state.status === 'ready'}
		<CatalogTable {rows} {total} {columns} selected={selectedIds} sort={filters.sort} order={filters.order} {resetToken}
			on:open={openRow} on:toggle={toggle} on:sort={(e) => setSort(e.detail)} on:selectAll={selectAll} on:clearSelection={clearSelection} />
		<p class="shrink-0 text-[10px] text-[#555]">
			↑↓ move · Enter opens · Space selects · Shift-click selects a range · Ctrl+A selects all loaded · Esc clears · / search. Dates are UTC.
		</p>
		{#if state.error}<p class="text-[10px] text-amber-500/80" role="status">Refresh failed ({state.error}); showing the last results.</p>{/if}
	{:else}
		<div class="border border-[#222] bg-[#050505]">
			<SectionState {state} what="The catalog" endpoint="GET /api/data/catalog" rows={10} on:retry={() => load()} />
		</div>
	{/if}
</div>
