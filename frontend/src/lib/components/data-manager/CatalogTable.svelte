<script lang="ts">
	// The catalog as a windowed grid: only the rows in view are in the DOM, so
	// 1,700+ series scroll smoothly. One tab stop; ↑/↓ PgUp/PgDn Home/End move,
	// Enter opens the series, Space selects, Ctrl+A selects everything loaded.
	// One scroll container with a sticky header: narrow windows scroll sideways.
	import { createEventDispatcher, onDestroy, onMount } from 'svelte';
	import type { CatalogRow } from '$lib/api/dataManagerTypes';
	import { clock } from '$lib/stores/dataManager';
	import StateChip from './StateChip.svelte';
	import { COLUMNS, GRID_GAP, gridMinWidth, gridTemplate, type ColumnKey, type SortKey } from './catalogViews';
	import { formatBytes, formatPercent, formatRelative, formatUtc, historyLength, isMarketWide, MARKET_WIDE_HELP, streamLabel, TIER_LABEL, venueLabel, venueShort } from './format';
	import { seriesHref } from './links';
	import { moveIndex, scrollTopFor, windowRange } from './windowing';

	/** Loaded rows; indexes at or past rows.length (up to total) are still loading. */
	export let rows: CatalogRow[] = [];
	export let total = 0;
	export let columns: ColumnKey[];
	export let selected: Set<string>;
	export let sort: SortKey;
	export let order: 'asc' | 'desc';
	/** Bump to scroll back to the top (a new query). */
	export let resetToken = 0;

	const ROW = 34;
	const dispatch = createEventDispatcher<{
		open: CatalogRow;
		toggle: { row: CatalogRow; index: number; shift: boolean };
		sort: SortKey;
		selectAll: void;
		clearSelection: void;
	}>();

	let viewport: HTMLDivElement | undefined;
	let headerHeight = 28;
	let scrollTop = 0;
	let height = 480;
	let active = -1;
	let appliedReset = resetToken;
	let resizeObserver: ResizeObserver | null = null;

	$: visibleColumns = COLUMNS.filter((c) => columns.includes(c.key));
	$: template = gridTemplate(columns);
	$: minWidth = gridMinWidth(columns);
	$: bodyHeight = Math.max(ROW, height - headerHeight);
	$: win = windowRange(scrollTop, bodyHeight, ROW, total, 10);
	$: indices = Array.from({ length: Math.max(0, win.end - win.start) }, (_, k) => win.start + k);
	$: allLoadedSelected = rows.length > 0 && rows.every((row) => selected.has(row.id));
	$: if (resetToken !== appliedReset) {
		appliedReset = resetToken;
		active = -1;
		scrollTop = 0;
		if (viewport) viewport.scrollTop = 0;
	}
	$: if (active >= total) active = total - 1;

	onMount(() => {
		if (viewport && typeof ResizeObserver !== 'undefined') {
			resizeObserver = new ResizeObserver(() => (height = viewport?.clientHeight || height));
			resizeObserver.observe(viewport);
		}
		height = viewport?.clientHeight || height;
	});
	onDestroy(() => resizeObserver?.disconnect());

	function reveal(index: number) {
		if (!viewport) return;
		const next = scrollTopFor(index, viewport.scrollTop, bodyHeight, ROW);
		if (next != null) viewport.scrollTop = next;
	}

	function onKey(event: KeyboardEvent) {
		const target = event.target as HTMLElement;
		if (target.tagName === 'INPUT' && event.key === ' ') return;
		const moved = moveIndex(active, event.key, total, Math.max(1, Math.floor(bodyHeight / ROW) - 1));
		if (moved != null) {
			event.preventDefault();
			active = moved;
			reveal(moved);
			return;
		}
		const row = rows[active];
		if (event.key === 'Enter' && row) {
			event.preventDefault();
			dispatch('open', row);
		} else if (event.key === ' ' && row) {
			event.preventDefault();
			dispatch('toggle', { row, index: active, shift: event.shiftKey });
		} else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
			event.preventDefault();
			dispatch('selectAll');
		} else if (event.key === 'Escape' && selected.size) {
			event.preventDefault();
			dispatch('clearSelection');
		}
	}

	function onFocus() {
		if (active < 0 && total > 0) active = Math.min(total - 1, Math.floor(scrollTop / ROW));
	}

	function rowClick(event: MouseEvent, row: CatalogRow, index: number) {
		active = index;
		// Keep keyboard focus on the grid: a focused row may be windowed out of the DOM.
		viewport?.focus({ preventScroll: true });
		const target = event.target as HTMLElement;
		if (target.closest('input, a, button')) return;
		if (event.shiftKey || event.ctrlKey || event.metaKey) {
			dispatch('toggle', { row, index, shift: event.shiftKey });
			return;
		}
		dispatch('open', row);
	}

	const sortState = (key: SortKey | undefined) => (key && key === sort ? (order === 'asc' ? 'ascending' : 'descending') : undefined);
	const consumerTitle = (row: CatalogRow) =>
		row.consumers.top.map((c) => `${c.id} ${c.name}${c.stage ? ` (${c.stage})` : c.status ? ` (${c.status})` : ''}`).join('\n') +
		(row.consumers.count > row.consumers.top.length ? `\n+${row.consumers.count - row.consumers.top.length} more` : '');
</script>

<div bind:this={viewport} on:scroll={() => (scrollTop = viewport?.scrollTop ?? 0)}
	role="grid" tabindex="0" aria-label="Stored series" aria-rowcount={total + 1} aria-multiselectable="true"
	aria-activedescendant={active >= 0 && rows[active] ? `dm-cat-row-${active}` : undefined}
	on:keydown={onKey} on:focus={onFocus} data-testid="catalog-grid"
	class="rounded-md relative min-h-0 flex-1 overflow-auto border border-sc-line bg-sc-panel outline-none focus-visible:border-sc-line2">
	<div role="rowgroup" bind:clientHeight={headerHeight} class="sticky top-0 z-10 border-b border-sc-line bg-sc-panel" style="min-width: {minWidth}px">
		<div role="row" aria-rowindex={1} class="grid items-center px-3 py-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3" style="grid-template-columns: {template}; column-gap: {GRID_GAP}px">
			<div role="columnheader" class="flex items-center">
				<input type="checkbox" tabindex="-1" checked={allLoadedSelected} on:change={() => dispatch(allLoadedSelected ? 'clearSelection' : 'selectAll')}
					aria-label={allLoadedSelected ? 'Clear the selection' : 'Select every loaded series'} class="accent-white" />
			</div>
			<div role="columnheader" aria-sort={sortState('symbol')} title="Research series (Binance USD-M perp) unless a venue is shown">
				<button type="button" tabindex="-1" on:click={() => dispatch('sort', 'symbol')} class="hover:text-sc-ink {sort === 'symbol' ? 'text-sc-ink' : ''}">
					Series{sort === 'symbol' ? (order === 'asc' ? ' ↑' : ' ↓') : ''}</button>
			</div>
			{#each visibleColumns as column (column.key)}
				<div role="columnheader" aria-sort={sortState(column.sort)} title={column.title} class="truncate {column.align === 'right' ? 'text-right' : ''}">
					{#if column.sort}
						<button type="button" tabindex="-1" on:click={() => column.sort && dispatch('sort', column.sort)}
							class="hover:text-sc-ink {sort === column.sort ? 'text-sc-ink' : ''}">
							{column.label}{sort === column.sort ? (order === 'asc' ? ' ↑' : ' ↓') : ''}</button>
					{:else}
						{column.label}
					{/if}
				</div>
			{/each}
		</div>
	</div>

	<div role="rowgroup" style="min-width: {minWidth}px">
		<div style="height: {win.padTop}px" role="presentation"></div>
		{#each indices as i (rows[i]?.id ?? `pending-${i}`)}
			{@const row = rows[i]}
			{#if row}
				<!-- Keyboard: the grid handles Enter and Space for the active row. -->
				<!-- svelte-ignore a11y-click-events-have-key-events -->
				<div role="row" id="dm-cat-row-{i}" tabindex="-1" aria-rowindex={i + 2} aria-selected={selected.has(row.id)} data-testid="catalog-row"
					on:click={(e) => rowClick(e, row, i)}
					class="grid h-[34px] cursor-pointer items-center border-b border-sc-line px-3 text-[11px] transition-colors {i === active ? 'bg-sc-ink/[0.06] shadow-[inset_2px_0_0_#fff]' : selected.has(row.id) ? 'bg-sky-500/[0.06]' : 'hover:bg-sc-ink/[0.025]'}"
					style="grid-template-columns: {template}; column-gap: {GRID_GAP}px">
					<div role="gridcell">
						<input type="checkbox" tabindex="-1" checked={selected.has(row.id)} class="accent-white"
							on:click|stopPropagation={(e) => dispatch('toggle', { row, index: i, shift: e.shiftKey })}
							aria-label={`Select ${row.display_symbol} ${row.timeframe} ${streamLabel(row.stream)}`} />
					</div>
					<div role="gridcell" class="flex min-w-0 items-center gap-1.5">
						<a href={seriesHref(row)} tabindex="-1" class="truncate font-bold text-sc-ink hover:underline">{row.display_symbol}</a>
						{#if row.venue !== 'canonical'}
							<span class="shrink-0 border border-sky-900 px-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sky-300" title={venueLabel(row.venue, row.source, row.market)}>{venueShort(row.venue)}</span>
						{/if}
						{#if row.delisted}<span class="shrink-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2" title={row.frozen_reason ?? 'Delisted'}>delisted</span>{/if}
						{#if row.asset_class !== 'crypto'}<span class="shrink-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{row.asset_class}</span>{/if}
					</div>
					{#each visibleColumns as column (column.key)}
						{#if column.key === 'tf'}
							<div role="gridcell" class="font-mono text-sc-ink">{row.timeframe}</div>
						{:else if column.key === 'stream'}
							<div role="gridcell" class="truncate text-[10px] text-sc-ink2">{streamLabel(row.stream)}</div>
						{:else if column.key === 'history'}
							<div role="gridcell" class="truncate font-mono text-[10px] tabular-nums text-sc-ink2" title={row.first_ts ? `${formatUtc(row.first_ts)} → ${formatUtc(row.last_ts)}` : 'Nothing stored'}>
								{#if row.first_ts}{formatUtc(row.first_ts, { date: true })} <span class="text-sc-ink">{historyLength(row.first_ts, row.last_ts)}</span>{:else}—{/if}
							</div>
						{:else if column.key === 'completeness'}
							<div role="gridcell" class="flex items-center gap-1.5" title={row.gap_count ? `${row.rows.toLocaleString('en-US')} of ${(row.expected_rows ?? 0).toLocaleString('en-US')} bars · ${row.gap_count.toLocaleString('en-US')} gaps, largest ${row.largest_gap_bars} bars` : `${row.rows.toLocaleString('en-US')} bars, no gaps`}>
								{#if row.completeness != null}
									<div class="h-1 w-8 shrink-0 bg-sc-raise"><div class="h-full bg-[#9ca3af]" style="width: {row.completeness * 100}%"></div></div>
									<span class="font-mono text-[10px] tabular-nums text-sc-ink2">{formatPercent(row.completeness)}</span>
								{:else}<span class="text-sc-ink3">—</span>{/if}
							</div>
						{:else if column.key === 'freshness'}
							<div role="gridcell" class="min-w-0"><StateChip state={row.sla.state} sla={row.sla} caption /></div>
						{:else if column.key === 'quality'}
							<div role="gridcell" class="text-right font-mono tabular-nums" title={row.quality.issues.length ? row.quality.issues.join('\n') : row.quality.score == null ? 'Not scored yet' : 'No issues found'}>
								{#if row.quality.score == null}<span class="text-sc-ink3">—</span>{:else}<span class="text-sc-ink">{Math.round(row.quality.score)}</span>{#if row.quality.issues.length}<span class="text-[9px] text-sc-ink2">·{row.quality.issues.length}</span>{/if}{/if}
							</div>
						{:else if column.key === 'consumers'}
							<div role="gridcell" class="flex min-w-0 items-center gap-1.5" title={row.consumers.count ? consumerTitle(row) : isMarketWide(row) ? MARKET_WIDE_HELP : 'Nothing reads it'}>
								{#if row.consumers.count}
									<span class="shrink-0 border border-sc-line2 px-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">{TIER_LABEL[row.consumers.tier]}</span>
									<span class="truncate font-mono text-[10px] text-sc-ink2">{row.consumers.top[0]?.id ?? ''}{row.consumers.count > 1 ? ` +${row.consumers.count - 1}` : ''}</span>
								{:else if isMarketWide(row)}
									<span class="shrink-0 border border-sc-line2 px-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">{TIER_LABEL[row.sla.tier]}</span>
									<span class="truncate text-[10px] text-sc-ink2">market-wide</span>
								{:else}<span class="text-sc-ink4">—</span>{/if}
							</div>
						{:else if column.key === 'size'}
							<div role="gridcell" class="text-right font-mono text-[10px] tabular-nums text-sc-ink2">{formatBytes(row.size_bytes)}</div>
						{:else if column.key === 'updated'}
							<div role="gridcell" class="truncate text-right text-[10px] text-sc-ink3" title={formatUtc(row.updated_at, { seconds: true })}>{row.updated_at ? formatRelative(row.updated_at, $clock) : '—'}</div>
						{/if}
					{/each}
				</div>
			{:else}
				<div role="row" aria-rowindex={i + 2} aria-busy="true" class="flex h-[34px] items-center gap-3 border-b border-sc-line px-3">
					<div class="h-2.5 w-32 animate-pulse bg-sc-raise"></div>
					<div class="h-2.5 w-full animate-pulse bg-sc-panel2"></div>
				</div>
			{/if}
		{/each}
		<div style="height: {win.padBottom}px" role="presentation"></div>
	</div>
</div>
