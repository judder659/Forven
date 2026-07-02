<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { getRankedDataGaps, type DataGapSummary } from '$lib/api';

	let items: DataGapSummary[] = [];
	let loading = true;
	let error: string | null = null;
	let search = '';
	let limit = 50;
	let categoryFilter = '';
	let sortField: 'priority' | 'requests' | 'title' = 'priority';
	let sortDirection: 'asc' | 'desc' = 'desc';
	let urlReady = false;

	function syncUrl(): void {
		if (!urlReady || typeof window === 'undefined') return;
		const url = new URL(window.location.href);
		const params = url.searchParams;
		const set = (key: string, value: string, fallback: string) => {
			if (value && value !== fallback) params.set(key, value);
			else params.delete(key);
		};
		set('q', search.trim(), '');
		set('category', categoryFilter, '');
		set('limit', String(limit), '50');
		set('sort', sortField, 'priority');
		set('dir', sortDirection, 'desc');
		history.replaceState(history.state, '', url.toString());
	}

	// Persist view state to the URL query string (reproducible / shareable / survives reload).
	$: if (urlReady) {
		// reference reactive deps so the block re-runs when any of them change
		void [search, categoryFilter, limit, sortField, sortDirection];
		syncUrl();
	}

	async function load(): Promise<void> {
		loading = true;
		error = null;
		try {
			const res = await getRankedDataGaps(limit);
			items = res.items ?? [];
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load data gaps.';
		} finally {
			loading = false;
		}
	}

	function toggleSort(field: 'priority' | 'requests' | 'title'): void {
		if (sortField === field) {
			sortDirection = sortDirection === 'desc' ? 'asc' : 'desc';
			return;
		}
		sortField = field;
		sortDirection = field === 'title' ? 'asc' : 'desc';
	}

	function sortIndicator(field: 'priority' | 'requests' | 'title'): string {
		if (sortField !== field) return '';
		return sortDirection === 'desc' ? ' ▼' : ' ▲';
	}

	$: categories = Array.from(
		new Set(items.map((item) => (item.category ?? '').trim()).filter(Boolean)),
	).sort((a, b) => a.localeCompare(b));

	$: filtered = (() => {
		const query = search.trim().toLowerCase();
		let base = categoryFilter
			? items.filter((item) => (item.category ?? '') === categoryFilter)
			: items.slice();
		if (query) {
			base = base.filter(
				(item) =>
					item.title.toLowerCase().includes(query) ||
					item.missing_dataset.toLowerCase().includes(query) ||
					(item.category ?? '').toLowerCase().includes(query) ||
					(item.why_it_matters ?? '').toLowerCase().includes(query) ||
					item.missing_fields.some((f) => f.toLowerCase().includes(query)),
			);
		}
		const dir = sortDirection === 'desc' ? -1 : 1;
		base.sort((a, b) => {
			if (sortField === 'title') {
				return a.title.localeCompare(b.title) * dir;
			}
			if (sortField === 'requests') {
				return (a.request_count - b.request_count) * dir;
			}
			return (a.priority_score - b.priority_score) * dir;
		});
		return base;
	})();

	// True when a search/category yields nothing AND the server fetch is capped — raising
	// "Fetch" might surface a gap that is ranked below the current limit.
	$: cappedNoMatch =
		!loading &&
		filtered.length === 0 &&
		(search.trim() !== '' || categoryFilter !== '') &&
		items.length >= limit;

	onMount(() => {
		const params = $page?.url?.searchParams;
		if (params) {
			search = params.get('q') ?? '';
			categoryFilter = params.get('category') ?? '';
			const parsedLimit = Number(params.get('limit'));
			if ([20, 50, 100, 200].includes(parsedLimit)) limit = parsedLimit;
			const sort = params.get('sort');
			if (sort === 'priority' || sort === 'requests' || sort === 'title') sortField = sort;
			const dir = params.get('dir');
			if (dir === 'asc' || dir === 'desc') sortDirection = dir;
		}
		urlReady = true;
		void load();
	});
</script>

<svelte:head>
	<title>Data Gaps | Forven</title>
</svelte:head>

<div class="h-full flex flex-col overflow-y-auto bg-[#050505] text-white">
	<div class="px-4 py-3 border-b border-[#222] flex-shrink-0">
		<div class="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
			<div>
				<div class="inline-flex items-center gap-2 border border-[#333] px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-[#888]">
					<span class="h-1.5 w-1.5 rounded-full bg-[#555]"></span>
					Research / Data Gaps
				</div>
				<h1 class="mt-3 text-lg font-bold uppercase tracking-widest text-white">Data Gaps</h1>
				<p class="mt-1 text-xs text-[#666] max-w-2xl">
					Missing datasets and fields most often blocking crucible execution, ranked by priority and request volume.
				</p>
				<p class="mt-1 text-[10px] text-[#555] max-w-2xl">
					Priority is a composite score (request volume × recency) — higher means more urgent. Read it as a relative ranking, not an absolute scale.
				</p>
			</div>
			<div class="flex items-center gap-2">
				<a
					href="/hypotheses"
					class="text-xs border border-[#333] px-3 py-1.5 text-[#888] hover:text-white hover:border-white transition-colors"
				>
					← Back to Crucibles
				</a>
				<button
					type="button"
					on:click={load}
					class="text-xs border border-[#333] px-3 py-1.5 text-[#888] hover:text-white hover:border-white transition-colors"
				>
					Refresh
				</button>
			</div>
		</div>
	</div>

	<!-- Toolbar -->
	<div class="border-b border-[#222] px-4 py-2 flex items-center gap-2 flex-wrap">
		<input
			type="text"
			bind:value={search}
			placeholder="Search title, dataset, category, fields, rationale…"
			class="bg-black border border-[#333] px-3 py-1.5 text-xs w-80 focus:outline-none focus:border-white"
		/>
		{#if categories.length}
			<select
				bind:value={categoryFilter}
				aria-label="Filter by category"
				class="bg-black border border-[#333] px-2 py-1.5 text-xs text-[#888] focus:outline-none focus:border-white"
			>
				<option value="">All categories</option>
				{#each categories as cat}
					<option value={cat}>{cat}</option>
				{/each}
			</select>
		{/if}
		<span class="text-[10px] text-[#666] ml-1">
			{filtered.length} items
			{#if filtered.length !== items.length}
				(of {items.length})
			{/if}
		</span>
		<div class="ml-auto flex items-center gap-2 text-[10px] text-[#666]">
			<label for="datagaps-limit">Fetch</label>
			<select
				id="datagaps-limit"
				class="bg-black border border-[#333] px-2 py-1 text-xs text-[#888]"
				bind:value={limit}
				on:change={load}
			>
				<option value={20}>20</option>
				<option value={50}>50</option>
				<option value={100}>100</option>
				<option value={200}>200</option>
			</select>
		</div>
	</div>

	{#if error}
		<div class="mx-4 mt-3 border border-red-900 bg-red-500/5 text-red-400 text-xs px-3 py-2">{error}</div>
	{/if}

	<div class="flex-1 overflow-auto bg-black">
		<table class="w-full text-xs">
			<thead class="sticky top-0 bg-[#050505] z-10">
				<tr class="text-[#666] border-b border-[#222]">
					<th class="py-2 px-3 text-left w-10">#</th>
					<th class="py-2 px-3 text-left cursor-pointer" on:click={() => toggleSort('title')}>Gap{sortIndicator('title')}</th>
					<th class="py-2 px-3 text-left">Category</th>
					<th class="py-2 px-3 text-left">Dataset</th>
					<th class="py-2 px-3 text-left">Missing Fields</th>
					<th
						class="py-2 px-3 text-right cursor-pointer"
						on:click={() => toggleSort('requests')}
						title="Number of crucibles that requested this dataset/field. Higher = more widely blocking."
					>Requests{sortIndicator('requests')}</th>
					<th
						class="py-2 px-3 text-right cursor-pointer"
						on:click={() => toggleSort('priority')}
						title="Composite rank derived from request volume and recency (higher = more urgent). Use sort to compare relative urgency rather than reading the absolute value."
					>Priority{sortIndicator('priority')}</th>
				</tr>
			</thead>
			<tbody>
				{#if loading}
					<tr><td colspan="7" class="py-8 text-center text-[#555]">Loading data gaps…</td></tr>
				{:else if filtered.length === 0}
					<tr>
						<td colspan="7" class="py-8 text-center text-[#555]">
							<div>No data gaps match this view.</div>
							{#if cappedNoMatch}
								<div class="mt-2 text-[11px] text-yellow-400">
									Showing the top {limit} ranked gaps only — raise “Fetch” to search lower-priority gaps.
								</div>
							{/if}
						</td>
					</tr>
				{:else}
					{#each filtered as item, index (item.id)}
						<tr class="border-t border-[#111] hover:bg-[#111] align-top">
							<td class="py-2 px-3 text-[#666] font-mono">{index + 1}</td>
							<td class="py-2 px-3">
								<div class="text-white font-medium">{item.title}</div>
								{#if item.why_it_matters}
									<div class="mt-1 text-[#666] leading-5 max-w-2xl">{item.why_it_matters}</div>
								{/if}
							</td>
							<td class="py-2 px-3">
								{#if item.category}
									<span class="border border-[#222] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[#666]">{item.category}</span>
								{:else}
									<span class="text-[#555]">—</span>
								{/if}
							</td>
							<td class="py-2 px-3 text-[#888] font-mono uppercase tracking-wider text-[10px]">{item.missing_dataset}</td>
							<td class="py-2 px-3">
								{#if item.missing_fields.length}
									<div class="flex flex-wrap gap-1">
										{#each item.missing_fields as field}
											<span class="border border-[#222] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[#666]">{field}</span>
										{/each}
									</div>
								{:else}
									<span class="text-[#555]">—</span>
								{/if}
							</td>
							<td class="py-2 px-3 text-right align-top">
								{#if item.requesting_hypotheses && item.requesting_hypotheses.length}
									<div class="flex flex-col items-end gap-1">
										<div class="font-mono text-white">{item.request_count}</div>
										<div class="flex flex-wrap justify-end gap-1">
											{#each item.requesting_hypotheses.slice(0, 4) as req (req.id)}
												<a
													href={`/hypotheses/${encodeURIComponent(req.id)}`}
													title={req.title}
													class="border border-[#333] px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider text-[#888] hover:border-white hover:text-white"
												>{req.display_id ?? req.id}</a>
											{/each}
											{#if item.requesting_hypotheses.length > 4}
												<span class="px-1 text-[10px] text-[#666]">+{item.requesting_hypotheses.length - 4}</span>
											{/if}
										</div>
									</div>
								{:else}
									<span class="font-mono text-white">{item.request_count}</span>
								{/if}
							</td>
							<td class="py-2 px-3 text-right font-mono text-white">{item.priority_score.toFixed(1)}</td>
						</tr>
					{/each}
				{/if}
			</tbody>
		</table>
	</div>
</div>
