<script lang="ts">
	import { onMount } from 'svelte';

	import {
		archiveHypothesis,
		bulkArchiveHypotheses,
		bulkRestoreHypotheses,
		bulkTrashHypotheses,
		discoverCrucibles,
		forceRevisitHypothesis,
		getHypotheses,
		restoreHypothesis,
		retriggerHypothesisResearch,
		trashHypothesis,
		type HypothesisManagerView,
		type HypothesisQuality,
		type HypothesisSummary,
	} from '$lib/api';
	import { getHypothesisCounts } from '$lib/api/hypotheses';
	import { goto } from '$app/navigation';

	import HypothesisTable from '$lib/components/hypotheses/HypothesisTable.svelte';
	import ManualIngestDialog from '$lib/components/hypotheses/ManualIngestDialog.svelte';
	import UrlIngestDialog from '$lib/components/hypotheses/UrlIngestDialog.svelte';

	type BannerTone = 'success' | 'error';

	interface BannerState {
		tone: BannerTone;
		message: string;
	}

	let hypotheses: HypothesisSummary[] = [];
	let counts: Record<HypothesisManagerView, number> = {
		active: 0,
		archived: 0,
		trash: 0,
		graduated: 0,
	};
	let loading = true;
	let error: string | null = null;
	let managerView: HypothesisManagerView = 'active';
	let laneFilter = '';
	let statusFilter = '';
	let qualityFilter: HypothesisQuality | '' = '';
	let searchQuery = '';
	let sortOption = 'updated_desc';
	let includeDisproven = false;
	let selectedIds = new Set<string>();
	let mutationPending = false;
	let banner: BannerState | null = null;
	let urlIngestOpen = false;
	let manualIngestOpen = false;
	let discoverPending = false;
	let bulkResearchProgress: { done: number; total: number } | null = null;
	let pageSize = 50;
	let currentPage = 1;
	let totalRows = 0;
	let lastViewSignature = '';

	const viewLabels: Record<HypothesisManagerView, string> = {
		active: 'Active',
		archived: 'Archived',
		trash: 'Trash',
		graduated: 'Graduated',
	};

	const viewDescriptions: Record<HypothesisManagerView, string> = {
		active: 'Current research inventory',
		archived: 'Parked but recoverable',
		trash: 'Queued for removal',
		graduated: 'Promoted to strategy work',
	};
	const managerViews: HypothesisManagerView[] = ['active', 'archived', 'trash', 'graduated'];

	async function loadCounts(): Promise<void> {
		try {
			// Single lightweight call replaces the previous four full-list fetches.
			const res = await getHypothesisCounts();
			const c = res.counts ?? {};
			counts = {
				active: c.active ?? 0,
				archived: c.archived ?? 0,
				trash: c.trash ?? 0,
				graduated: c.graduated ?? 0,
			};
		} catch {
			// Non-critical — leave previous counts.
		}
	}

	async function loadSurface(): Promise<void> {
		loading = true;
		error = null;
		try {
			const hypothesisResponse = await getHypotheses({
				view: managerView,
				lane: laneFilter || undefined,
				status: statusFilter || undefined,
				search: searchQuery || undefined,
				sort: sortOption || undefined,
				quality: qualityFilter || undefined,
				include_disproven: includeDisproven || statusFilter === 'disproven' || managerView === 'archived',
				limit: pageSize,
				offset: (currentPage - 1) * pageSize,
			});
			hypotheses = hypothesisResponse.hypotheses ?? [];
			totalRows = hypothesisResponse.total ?? hypotheses.length;
			// If the current page fell past the end (e.g. after a delete), step back and reload.
			const maxPage = Math.max(1, Math.ceil(totalRows / pageSize));
			if (currentPage > maxPage) {
				currentPage = maxPage;
				await loadSurface();
				return;
			}
			selectedIds = new Set(Array.from(selectedIds).filter((id) => hypotheses.some((item) => item.id === id)));
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load hypotheses.';
		} finally {
			loading = false;
		}
	}

	async function refreshAll(): Promise<void> {
		await Promise.all([loadSurface(), loadCounts()]);
	}

	function setBanner(tone: BannerTone, message: string): void {
		banner = { tone, message };
	}

	function setManagerView(view: HypothesisManagerView): void {
		if (managerView === view) return;
		managerView = view;
		selectedIds = new Set();
		void loadSurface();
	}

	function clearFilters(): void {
		laneFilter = '';
		statusFilter = '';
		qualityFilter = '';
		searchQuery = '';
		includeDisproven = false;
		sortOption = 'updated_desc';
	}

	function toggleSelect(hypothesisId: string): void {
		const next = new Set(selectedIds);
		if (next.has(hypothesisId)) {
			next.delete(hypothesisId);
		} else {
			next.add(hypothesisId);
		}
		selectedIds = next;
	}

	function toggleSelectAll(): void {
		if (!pageRows.length) return;
		if (pageRows.every((item) => selectedIds.has(item.id))) {
			const next = new Set(selectedIds);
			for (const row of pageRows) next.delete(row.id);
			selectedIds = next;
			return;
		}
		const next = new Set(selectedIds);
		for (const row of pageRows) next.add(row.id);
		selectedIds = next;
	}

	function clearSelection(): void {
		selectedIds = new Set();
	}

	async function runSingleAction(action: 'archive' | 'trash' | 'restore', hypothesisId: string): Promise<void> {
		mutationPending = true;
		banner = null;
		try {
			if (action === 'archive') {
				await archiveHypothesis(hypothesisId);
				setBanner('success', 'Crucible archived.');
			} else if (action === 'trash') {
				await trashHypothesis(hypothesisId);
				setBanner('success', 'Crucible moved to trash.');
			} else if (managerView === 'graduated') {
				// Graduated items re-enter via the revisit path, which enforces the
				// active-pool cap (409) and records revisit bookkeeping — plain restore would bypass both.
				await forceRevisitHypothesis(hypothesisId);
				setBanner('success', 'Crucible revisited and returned to active research.');
			} else {
				await restoreHypothesis(hypothesisId);
				setBanner('success', 'Crucible restored.');
			}
			selectedIds = new Set(Array.from(selectedIds).filter((id) => id !== hypothesisId));
			await refreshAll();
		} catch (err) {
			setBanner('error', err instanceof Error ? err.message : 'Lifecycle action failed.');
		} finally {
			mutationPending = false;
		}
	}

	async function runBulkAction(action: 'archive' | 'trash' | 'restore'): Promise<void> {
		const ids = Array.from(selectedIds);
		if (!ids.length) return;
		mutationPending = true;
		banner = null;
		try {
			if (action === 'archive') {
				await bulkArchiveHypotheses(ids);
				setBanner('success', 'Selected crucibles archived.');
			} else if (action === 'trash') {
				await bulkTrashHypotheses(ids);
				setBanner('success', 'Selected crucibles moved to trash.');
			} else if (managerView === 'graduated') {
				// No bulk-revisit endpoint; revisit each so the active-pool cap is honoured per item.
				let revisited = 0;
				let blocked = 0;
				let failed = 0;
				for (const id of ids) {
					try {
						await forceRevisitHypothesis(id);
						revisited += 1;
					} catch (err) {
						if (err instanceof Error && /pool/i.test(err.message)) blocked += 1;
						else failed += 1;
					}
				}
				const parts = [`${revisited} revisited`];
				if (blocked) parts.push(`${blocked} blocked (active pool full)`);
				if (failed) parts.push(`${failed} failed`);
				setBanner(blocked || failed ? 'error' : 'success', parts.join(', ') + '.');
			} else {
				await bulkRestoreHypotheses(ids);
				setBanner('success', 'Selected crucibles restored.');
			}
			selectedIds = new Set();
			await refreshAll();
		} catch (err) {
			setBanner('error', err instanceof Error ? err.message : 'Bulk lifecycle action failed.');
		} finally {
			mutationPending = false;
		}
	}

	function handleCreated(event: CustomEvent<{ id: string }>): void {
		setBanner('success', 'Crucible created from URL.');
		void goto(`/hypotheses/${event.detail.id}`);
	}

	function handleCreatedBulk(event: CustomEvent<{ ids: string[] }>): void {
		const n = event.detail.ids.length;
		setBanner('success', `Created ${n} crucible${n === 1 ? '' : 's'} from URLs.`);
		void refreshAll();
	}

	function handleManualCreated(event: CustomEvent<{ id: string }>): void {
		setBanner('success', 'Crucible created.');
		void goto(`/hypotheses/${event.detail.id}`);
	}

	async function runRowResearch(hypothesisId: string): Promise<void> {
		mutationPending = true;
		banner = null;
		try {
			const res = await retriggerHypothesisResearch(hypothesisId);
			if (res.already_running) {
				setBanner('success', 'Research already queued for this crucible.');
			} else {
				setBanner('success', 'Research task queued.');
			}
			await loadSurface();
		} catch (err) {
			setBanner('error', err instanceof Error ? err.message : 'Failed to queue research.');
		} finally {
			mutationPending = false;
		}
	}

	async function runDiscovery(): Promise<void> {
		discoverPending = true;
		banner = null;
		try {
			const res = await discoverCrucibles();
			if (res.created) {
				setBanner(
					'success',
					`Discovery dispatched${res.mode ? ` (${res.mode})` : ''}. Harvested ideas appear as Proposed crucibles for review.`,
				);
			} else if (res.reason === 'already_open') {
				setBanner('success', 'A discovery run is already in progress.');
			} else {
				setBanner('error', `Could not start discovery: ${res.reason ?? 'unknown'}.`);
			}
			await refreshAll();
		} catch (err) {
			setBanner('error', err instanceof Error ? err.message : 'Failed to start discovery.');
		} finally {
			discoverPending = false;
		}
	}

	async function bulkResearchPlaceholders(): Promise<void> {
		const stuck = hypotheses.filter(
			(h) => h.manager_state === 'active' && h.quality === 'placeholder',
		);
		if (!stuck.length) return;
		mutationPending = true;
		banner = null;
		let queued = 0;
		let failed = 0;
		bulkResearchProgress = { done: 0, total: stuck.length };
		for (const hyp of stuck) {
			try {
				await retriggerHypothesisResearch(hyp.id);
				queued += 1;
			} catch {
				failed += 1;
			}
			bulkResearchProgress = { done: queued + failed, total: stuck.length };
		}
		bulkResearchProgress = null;
		setBanner(
			failed ? 'error' : 'success',
			`Queued ${queued} placeholder${queued === 1 ? '' : 's'} for research${failed ? `, ${failed} failed` : ''}.`,
		);
		await loadSurface();
		mutationPending = false;
	}

	function goPrevPage() {
		if (currentPage <= 1) return;
		currentPage = currentPage - 1;
		void loadSurface();
	}
	function goNextPage() {
		if (currentPage >= pageCount) return;
		currentPage = currentPage + 1;
		void loadSurface();
	}
	function changePageSize(value: number): void {
		pageSize = Number.isFinite(value) && value > 0 ? value : 50;
		currentPage = 1;
		void loadSurface();
	}

	// Reload when filters/search/sort change (debounced implicitly by Svelte reactivity batching)
	$: filterSignature = [managerView, laneFilter, statusFilter, qualityFilter, searchQuery, sortOption, String(includeDisproven)].join('|');
	$: if (filterSignature) {
		// Fire when signature mutates after initial mount.
		void triggerReload(filterSignature);
	}
	let initialized = false;
	async function triggerReload(sig: string) {
		if (!initialized) { return; }
		if (sig === lastViewSignature) return;
		lastViewSignature = sig;
		currentPage = 1;
		await loadSurface();
	}

	// Server-side pagination: `hypotheses` holds exactly the current page; `totalRows`
	// is the full filtered bucket size. Header stats below are scoped to the visible
	// page (the "reflects the filtered view, not the full bucket" disclaimer applies).
	$: pageCount = Math.max(1, Math.ceil(totalRows / pageSize));
	$: pageRows = hypotheses;
	$: selectedInView = pageRows.reduce((count, row) => (selectedIds.has(row.id) ? count + 1 : count), 0);
	$: placeholderCount = hypotheses.filter(
		(h) => h.manager_state === 'active' && h.quality === 'placeholder',
	).length;
	$: selectedCount = selectedIds.size;
	$: activeTaskCount = hypotheses.filter((h) => h.active_task).length;
	$: dataGapCount = hypotheses.reduce((total, h) => total + (h.open_data_gap_count ?? 0), 0);
	$: provenCount = hypotheses.filter((h) => h.status === 'proven').length;
	$: productiveCount = hypotheses.filter((h) => h.quality === 'productive').length;
	$: researchCount = hypotheses.filter((h) => h.status === 'researching' || h.quality === 'researching').length;
	$: filtersActive = Boolean(laneFilter || statusFilter || qualityFilter || searchQuery || includeDisproven || sortOption !== 'updated_desc');
	$: visibleStart = totalRows === 0 ? 0 : (currentPage - 1) * pageSize + 1;
	$: visibleEnd = Math.min((currentPage - 1) * pageSize + hypotheses.length, totalRows);

	onMount(() => {
		lastViewSignature = filterSignature;
		void (async () => {
			await refreshAll();
			initialized = true;
		})();
	});
</script>

<svelte:head>
	<title>Crucibles | Forven</title>
</svelte:head>

<div class="h-full flex flex-col overflow-hidden bg-[#050505] text-gray-100">
	<div class="flex-shrink-0 border-b border-[#222] bg-black">
		<div class="px-4 py-4">
			<div class="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
				<div class="min-w-0">
					<div class="inline-flex items-center gap-2 border border-[#333] px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-[#888]">
						<span class="h-1.5 w-1.5 rounded-full bg-[#555]"></span>
						Research / Crucibles
					</div>
					<h1 class="mt-3 text-lg font-bold uppercase tracking-widest text-white">The Crucible</h1>
					<p class="mt-1 text-[11px] text-[#666]">An idea under test — proposed by an agent, harvested from a source, or seeded by you — that the Forge proves or disproves.</p>
					<p class="mt-1 max-w-3xl text-xs leading-relaxed text-[#666]">
						Operate the thesis inventory from intake to verdict. Prioritize active research, repair weak entries, and keep data blockers visible before they stall the strategy pipeline.
					</p>
				</div>
				<div class="flex flex-wrap items-center gap-2">
					<a
						href="/hypotheses/data-gaps"
						class="border border-[#333] px-3 py-1.5 text-xs text-[#888] transition-colors hover:border-white hover:text-white"
					>
						Data gaps
					</a>
					<a
						href="/admin/hypotheses/cleanup"
						class="border border-[#333] px-3 py-1.5 text-xs text-[#888] transition-colors hover:border-white hover:text-white"
						title="Evidence cleanup + LLM triage for low-quality crucibles"
					>
						Triage
					</a>
					<button
						type="button"
						data-action="discover-crucibles"
						on:click={runDiscovery}
						disabled={discoverPending}
						class="terminal-button text-xs"
						title="Harvest new crucibles from external sources (YouTube/Reddit/forums/podcasts)"
					>
						{discoverPending ? 'Discovering…' : 'Discover'}
					</button>
					<button
						type="button"
						on:click={() => (manualIngestOpen = true)}
						class="terminal-button text-xs"
					>
						Create manually
					</button>
					<button
						type="button"
						on:click={() => (urlIngestOpen = true)}
						class="terminal-button-primary text-xs"
					>
						Add URL
					</button>
					<button
						type="button"
						on:click={refreshAll}
						class="border border-[#333] px-3 py-1.5 text-xs text-[#888] transition-colors hover:border-white hover:text-white"
					>
						Refresh
					</button>
				</div>
			</div>

			<div class="mt-4 flex items-center gap-2 text-[10px] uppercase tracking-[0.18em] text-[#555]">
				<span>Inventory health</span>
				<span class="normal-case tracking-normal {filtersActive ? 'text-yellow-400/80' : 'text-[#555]'}">
					— {filtersActive ? 'reflects the filtered view, not the full bucket' : 'in current view'}
				</span>
			</div>
			<div class="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
				<div class="border border-[#222] bg-[#090909] px-3 py-2">
					<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Inventory</div>
					<div class="mt-1 flex items-end justify-between gap-3">
						<div class="text-2xl font-semibold text-white">{totalRows}</div>
						<div class="text-right text-[10px] uppercase tracking-[0.16em] text-[#555]">{viewLabels[managerView]}</div>
					</div>
				</div>
				<div class="border border-[#222] bg-[#090909] px-3 py-2">
					<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Research Queue</div>
					<div class="mt-1 flex items-end justify-between gap-3">
						<div class="text-2xl font-semibold text-white">{researchCount}</div>
						<div class="text-right text-[10px] uppercase tracking-[0.16em] text-[#555]">{activeTaskCount} active task{activeTaskCount === 1 ? '' : 's'}</div>
					</div>
				</div>
				<div class="border border-[#222] bg-[#090909] px-3 py-2">
					<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Quality</div>
					<div class="mt-1 flex items-end justify-between gap-3">
						<div class="text-2xl font-semibold {placeholderCount > 0 ? 'text-yellow-400' : 'text-emerald-200'}">{placeholderCount}</div>
						<div class="text-right text-[10px] uppercase tracking-[0.16em] text-[#555]">placeholder{placeholderCount === 1 ? '' : 's'} / {productiveCount} productive</div>
					</div>
				</div>
				<div class="border border-[#222] bg-[#090909] px-3 py-2">
					<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Blockers / Wins</div>
					<div class="mt-1 flex items-end justify-between gap-3">
						<div class="text-2xl font-semibold {dataGapCount > 0 ? 'text-yellow-400' : 'text-white'}">{dataGapCount}</div>
						<div class="text-right text-[10px] uppercase tracking-[0.16em] text-[#555]">{provenCount} proven</div>
					</div>
				</div>
			</div>
		</div>

		<div class="border-t border-[#181818] bg-[#070707] px-4 pt-3">
			<div class="flex flex-wrap items-end gap-1" role="tablist" aria-label="Crucible buckets">
				{#each managerViews as view}
					<button
						type="button"
						role="tab"
						aria-selected={managerView === view}
						on:click={() => setManagerView(view)}
						class="border-b-2 px-4 py-2 text-left transition-colors {managerView === view ? 'border-white text-white' : 'border-transparent text-[#555] hover:text-white'}"
					>
						<span class="block text-xs font-medium">{viewLabels[view]} <span class="text-[#555]">({counts[view]})</span></span>
						<span class="mt-0.5 block text-[10px] text-[#555]">{viewDescriptions[view]}</span>
					</button>
				{/each}
			</div>
		</div>

		<div class="border-t border-[#222] px-4 py-3">
			<div class="flex flex-wrap items-center gap-2">
				<div class="flex min-w-0 flex-1 flex-wrap items-center gap-2">
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search title, id, source, assets, timeframes..."
						class="min-w-[18rem] flex-1 border border-[#333] bg-black px-3 py-2 text-xs text-gray-100 placeholder:text-[#555] focus:border-white focus:outline-none"
					/>
					<select aria-label="Filter by stage" bind:value={statusFilter} class="w-44 border border-[#333] bg-black px-2 py-2 text-xs text-white">
						<option value="">All stages</option>
						<option value="proposed">Proposed</option>
						<option value="researching">Testing</option>
						<option value="proven">Viable / Expanded</option>
						<option value="disproven">Failed</option>
					</select>
					<select aria-label="Sort" bind:value={sortOption} class="w-36 border border-[#333] bg-black px-2 py-2 text-xs text-white">
						<option value="updated_desc">Updated first</option>
						<option value="created_desc">Newest first</option>
						<option value="novelty_desc">Highest novelty</option>
						<option value="title_asc">Title A-Z</option>
					</select>
				</div>

				<div class="flex flex-wrap items-center gap-2">
					<label class="flex items-center gap-1.5 border border-[#222] bg-[#090909] px-2 py-2 text-[10px] uppercase tracking-wider text-[#888]">
						<input type="checkbox" bind:checked={includeDisproven} class="h-3 w-3 accent-cyan-400" />
						Disproven
					</label>
					<button
						type="button"
						on:click={() => {
							managerView = 'active';
							qualityFilter = 'placeholder';
						}}
						class="border border-yellow-900 bg-yellow-500/5 px-2 py-2 text-[10px] uppercase tracking-[0.16em] text-yellow-400 hover:border-yellow-500"
					>
						Placeholders
					</button>
					<button
						type="button"
						on:click={() => (statusFilter = 'proven')}
						class="border border-emerald-700/50 bg-emerald-950/20 px-2 py-2 text-[10px] uppercase tracking-[0.16em] text-emerald-200 hover:bg-emerald-900/40"
					>
						Viable
					</button>
					<button
						type="button"
						on:click={clearFilters}
						disabled={!filtersActive}
						class="border border-[#333] px-2 py-2 text-[10px] uppercase tracking-[0.16em] text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
					>
						Clear
					</button>
				</div>
			</div>

			<div class="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-[#555]">
				<span>
					Showing {visibleStart}-{visibleEnd} of {totalRows}
				</span>
				<span class="hidden text-gray-700 sm:inline">/</span>
				<label for="hyp-page-size" class="uppercase tracking-[0.16em]">Rows</label>
				<select
					id="hyp-page-size"
					class="border border-[#333] bg-black px-2 py-1 text-xs text-white"
					value={String(pageSize)}
					on:change={(event) => changePageSize(Number((event.currentTarget as HTMLSelectElement).value))}
				>
					<option value="25">25</option>
					<option value="50">50</option>
					<option value="100">100</option>
					<option value="200">200</option>
				</select>
				<div class="ml-auto flex items-center gap-2">
					<button
						type="button"
						class="border border-[#333] px-2 py-1 text-[#888] hover:border-white hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
						on:click={goPrevPage}
						disabled={currentPage <= 1}
					>
						Prev
					</button>
					<span>{currentPage}/{pageCount}</span>
					<button
						type="button"
						class="border border-[#333] px-2 py-1 text-[#888] hover:border-white hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
						on:click={goNextPage}
						disabled={currentPage >= pageCount}
					>
						Next
					</button>
				</div>
			</div>
		</div>
	</div>

	{#if banner}
		<div class="mx-4 mt-3 border px-3 py-2 text-xs {banner.tone === 'success' ? 'border-emerald-700 bg-emerald-900/20 text-emerald-200' : 'border-rose-700 bg-rose-900/20 text-rose-200'}">
			{banner.message}
		</div>
	{/if}

	{#if error}
		<div class="mx-4 mt-3 bg-red-900/20 border border-red-800 text-red-300 text-xs px-3 py-2">{error}</div>
	{/if}

	{#if placeholderCount > 0 && managerView === 'active'}
		<div class="mx-4 mt-3 flex flex-wrap items-center justify-between gap-3 border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-300">
			<div>
				<strong class="font-semibold">{placeholderCount}</strong>
				crucible{placeholderCount === 1 ? '' : 's'} stuck as <em class="not-italic">placeholder{placeholderCount === 1 ? '' : 's'}</em>
				— agent hasn't extracted real fields yet.
			</div>
			<button
				type="button"
				on:click={bulkResearchPlaceholders}
				disabled={mutationPending}
				class="border border-yellow-900 bg-yellow-500/10 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-yellow-300 transition hover:border-yellow-500 disabled:opacity-50"
			>
				{#if bulkResearchProgress}
					Re-researching {bulkResearchProgress.done}/{bulkResearchProgress.total}…
				{:else}
					Re-research all
				{/if}
			</button>
		</div>
	{/if}

	<!-- Bulk-action bar: only visible when rows selected -->
	{#if selectedCount > 0}
		<div class="border-b border-[#222] bg-[#0c0c0c] px-4 py-2 flex items-center gap-3 text-xs">
			<span class="text-white font-medium">{selectedCount} selected</span>
			<button
				type="button"
				class="text-[#888] hover:text-white transition-colors"
				on:click={clearSelection}
			>
				Clear selection
			</button>
			<div class="ml-auto flex items-center gap-2">
				{#if managerView === 'active'}
					<button
						type="button"
						data-action="archive-selected"
						on:click={() => runBulkAction('archive')}
						disabled={mutationPending}
						class="px-2 py-1 border border-[#333] text-[#888] hover:border-white hover:text-white disabled:opacity-40"
					>
						Archive
					</button>
					<button
						type="button"
						data-action="trash-selected"
						on:click={() => runBulkAction('trash')}
						disabled={mutationPending}
						class="px-2 py-1 border border-yellow-700 text-yellow-300 hover:bg-yellow-900/20 disabled:opacity-40"
					>
						Trash
					</button>
				{:else if managerView === 'archived'}
					<button
						type="button"
						data-action="restore-selected"
						on:click={() => runBulkAction('restore')}
						disabled={mutationPending}
						class="px-2 py-1 border border-[#333] text-[#888] hover:border-white hover:text-white disabled:opacity-40"
					>
						Restore
					</button>
					<button
						type="button"
						data-action="trash-selected"
						on:click={() => runBulkAction('trash')}
						disabled={mutationPending}
						class="px-2 py-1 border border-yellow-700 text-yellow-300 hover:bg-yellow-900/20 disabled:opacity-40"
					>
						Trash
					</button>
				{:else if managerView === 'trash'}
					<button
						type="button"
						data-action="restore-selected"
						on:click={() => runBulkAction('restore')}
						disabled={mutationPending}
						class="px-2 py-1 border border-[#333] text-[#888] hover:border-white hover:text-white disabled:opacity-40"
					>
						Restore
					</button>
				{:else}
					<button
						type="button"
						data-action="revisit-selected"
						on:click={() => runBulkAction('restore')}
						disabled={mutationPending}
						title="Returns graduated crucibles to active research via the revisit path (subject to the active-pool cap)"
						class="px-2 py-1 border border-[#333] text-[#888] hover:border-white hover:text-white disabled:opacity-40"
					>
						Revisit
					</button>
				{/if}
			</div>
		</div>
	{/if}

	<div class="min-h-0 flex-1 overflow-auto">
		<HypothesisTable
			hypotheses={pageRows}
			{loading}
			{selectedIds}
			{mutationPending}
			emptyMessage="No crucibles match the current filters."
			onToggleSelect={toggleSelect}
			onToggleSelectAll={toggleSelectAll}
			onArchive={(hypothesisId) => runSingleAction('archive', hypothesisId)}
			onTrash={(hypothesisId) => runSingleAction('trash', hypothesisId)}
			onRestore={(hypothesisId) => runSingleAction('restore', hypothesisId)}
			onResearch={runRowResearch}
		/>
	</div>
</div>

<UrlIngestDialog
	open={urlIngestOpen}
	on:close={() => (urlIngestOpen = false)}
	on:created={handleCreated}
	on:createdBulk={handleCreatedBulk}
/>

<ManualIngestDialog
	open={manualIngestOpen}
	on:close={() => (manualIngestOpen = false)}
	on:created={handleManualCreated}
/>
