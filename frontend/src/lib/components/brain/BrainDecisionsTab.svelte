<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import {
		getBrainDecisions,
		type BrainDecisionRow,
		type BrainDecisionsListQuery
	} from '$lib/api/brain';
	import BrainDecisionDetailDrawer from './BrainDecisionDetailDrawer.svelte';

	const PAGE_SIZE = 50;

	let items: BrainDecisionRow[] = [];
	let total = 0;
	let offset = 0;
	let loading = false;
	let error = '';

	let cycleId = '';
	let actionType = '';
	let strategyId = '';
	let outcome = '';

	let selectedId: number | null = null;

	function readFiltersFromUrl(searchParams: URLSearchParams) {
		cycleId = searchParams.get('d_cycle') ?? '';
		actionType = searchParams.get('d_action') ?? '';
		strategyId = searchParams.get('d_strategy') ?? '';
		outcome = searchParams.get('d_outcome') ?? '';
		const sel = searchParams.get('d_id');
		selectedId = sel ? Number.parseInt(sel, 10) || null : null;
	}

	function writeFiltersToUrl() {
		const url = new URL($page.url);
		const setOrDelete = (key: string, value: string) => {
			if (value) url.searchParams.set(key, value);
			else url.searchParams.delete(key);
		};
		setOrDelete('d_cycle', cycleId);
		setOrDelete('d_action', actionType);
		setOrDelete('d_strategy', strategyId);
		setOrDelete('d_outcome', outcome);
		if (selectedId != null) url.searchParams.set('d_id', String(selectedId));
		else url.searchParams.delete('d_id');
		goto(url.pathname + url.search, {
			replaceState: true,
			keepFocus: true,
			noScroll: true
		});
	}

	function buildQuery(extraOffset = 0): BrainDecisionsListQuery {
		const q: BrainDecisionsListQuery = {
			limit: PAGE_SIZE,
			offset: extraOffset
		};
		if (cycleId.trim()) q.cycleId = cycleId.trim();
		if (actionType.trim()) q.actionType = actionType.trim();
		if (strategyId.trim()) q.strategyId = strategyId.trim();
		if (outcome) q.outcome = outcome;
		return q;
	}

	async function loadFirstPage() {
		loading = true;
		error = '';
		try {
			const resp = await getBrainDecisions(buildQuery(0));
			items = resp.items;
			total = resp.total;
			offset = resp.items.length;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			items = [];
			total = 0;
			offset = 0;
		} finally {
			loading = false;
		}
	}

	async function loadMore() {
		if (loading || items.length >= total) return;
		loading = true;
		try {
			const resp = await getBrainDecisions(buildQuery(offset));
			items = [...items, ...resp.items];
			offset = items.length;
			total = resp.total;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	function applyFilters() {
		writeFiltersToUrl();
		void loadFirstPage();
	}

	function clearFilters() {
		cycleId = '';
		actionType = '';
		strategyId = '';
		outcome = '';
		writeFiltersToUrl();
		void loadFirstPage();
	}

	function openDrawer(id: number) {
		selectedId = id;
		writeFiltersToUrl();
	}

	function closeDrawer() {
		selectedId = null;
		writeFiltersToUrl();
	}

	function previewSituation(value: string | null): string {
		if (!value) return '—';
		const max = 140;
		return value.length > max ? `${value.slice(0, max)}…` : value;
	}

	const CHIP = 'rounded border px-1.5 py-px font-plex-cond text-[10.5px] font-medium uppercase tracking-[0.06em]';
	const LABEL = 'flex flex-col gap-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3';
	const INPUT =
		'rounded-md border border-sc-line2 bg-sc-bg px-2 py-1.5 font-plex text-[13px] normal-case tracking-normal text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4';
	const BUTTON =
		'rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink disabled:cursor-not-allowed disabled:opacity-50';

	function outcomeClass(value: string | null): string {
		switch ((value ?? '').toLowerCase()) {
			case 'success':
				return `${CHIP} border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]`;
			case 'failure':
				return `${CHIP} border-[#e5574f]/40 bg-[#e5574f]/10 text-[#f2956f]`;
			case 'mixed':
				return `${CHIP} border-[#e7b24a]/40 bg-[#e7b24a]/10 text-[#e7b24a]`;
			default:
				return `${CHIP} border-sc-line2 bg-sc-raise text-sc-ink2`;
		}
	}

	function actionLabel(row: BrainDecisionRow): string {
		const decision = row.decision as { action_type?: string } | null;
		if (decision && typeof decision === 'object' && decision.action_type) {
			return decision.action_type;
		}
		return row.action_taken ? 'multi-action' : '—';
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}

	onMount(() => {
		readFiltersFromUrl($page.url.searchParams);
		void loadFirstPage();
	});

	$: if (typeof window !== 'undefined') {
		// React to back/forward navigation that changes the d_id param.
		const sel = $page.url.searchParams.get('d_id');
		const next = sel ? Number.parseInt(sel, 10) || null : null;
		if (next !== selectedId) selectedId = next;
	}
</script>

<div class="flex flex-col gap-3">
	<div class="grid grid-cols-1 items-end gap-3 rounded-md border border-sc-line bg-sc-panel p-3.5 sm:grid-cols-2 lg:grid-cols-[repeat(4,minmax(0,1fr))_auto]">
		<label class={LABEL}>
			<span>Cycle ID</span>
			<input
				type="text"
				class={INPUT}
				bind:value={cycleId}
				placeholder="e.g. cycle-2026-04-25-T03"
			/>
		</label>
		<label class={LABEL}>
			<span>Action type</span>
			<input
				type="text"
				class={INPUT}
				bind:value={actionType}
				placeholder="research, backtest, paper, …"
			/>
		</label>
		<label class={LABEL}>
			<span>Strategy ID</span>
			<input
				type="text"
				class={INPUT}
				bind:value={strategyId}
				placeholder="e.g. S00123"
			/>
		</label>
		<label class={LABEL}>
			<span>Outcome</span>
			<select class={INPUT} bind:value={outcome}>
				<option value="">All</option>
				<option value="success">Success</option>
				<option value="failure">Failure</option>
				<option value="mixed">Mixed</option>
			</select>
		</label>
		<div class="flex gap-2">
			<button
				type="button"
				class="rounded-md bg-sc-ink px-3 py-1.5 text-[12px] font-medium text-black hover:bg-white disabled:cursor-not-allowed disabled:opacity-40"
				on:click={applyFilters}
				disabled={loading}
			>
				Apply
			</button>
			<button type="button" class={BUTTON} on:click={clearFilters} disabled={loading}>Clear</button>
		</div>
	</div>

	{#if error}
		<div class="rounded-md border border-[#e5574f]/40 bg-[#e5574f]/10 px-3 py-2 text-[12.5px] text-[#f2956f]" role="alert">
			<strong class="font-medium">Failed to load decisions:</strong>
			{error}
			<button class="ml-2 underline transition-colors hover:text-sc-ink" type="button" on:click={() => loadFirstPage()}>retry</button>
		</div>
	{/if}

	{#if loading && items.length === 0}
		<div class="rounded-md border border-dashed border-sc-line p-4 text-center text-[13px] text-sc-ink3">Loading decisions…</div>
	{:else if items.length === 0 && !error}
		<div class="rounded-md border border-dashed border-sc-line p-4 text-center text-[13px] text-sc-ink3">
			No decisions yet. They appear here after the Brain's next cycle.
		</div>
	{:else}
		<div class="text-[12px] text-sc-ink3">{items.length} of {total} decisions</div>
		<ul class="m-0 flex list-none flex-col gap-2 p-0">
			{#each items as row (row.id)}
				<li>
					<button
						type="button"
						class="flex w-full flex-col gap-2 rounded-md border border-sc-line bg-sc-panel px-3.5 py-3 text-left text-sc-ink transition-colors hover:border-sc-line2 hover:bg-sc-hover"
						on:click={() => openDrawer(row.id)}
					>
						<div class="flex w-full flex-wrap items-center gap-2 text-[12px]">
							<span class="break-all font-plex-mono text-sc-ink">{row.cycle_id ?? '—'}</span>
							<span class={`${CHIP} border-sc-line2 bg-sc-raise text-sc-ink2`}>{actionLabel(row)}</span>
							<span class={outcomeClass(row.outcome_observed)}>
								{row.outcome_observed ?? 'pending'}
							</span>
							<span class="text-[11.5px] text-sc-ink3 sm:ml-auto">{formatTimestamp(row.created_at)}</span>
						</div>
						<div class="break-words text-[13px] leading-snug text-sc-ink2">{previewSituation(row.situation_summary)}</div>
					</button>
				</li>
			{/each}
		</ul>

		{#if items.length < total}
			<div class="flex justify-center py-2">
				<button type="button" class={BUTTON} on:click={loadMore} disabled={loading}>
					{loading ? 'Loading…' : `Load more (${total - items.length} remaining)`}
				</button>
			</div>
		{/if}
	{/if}
</div>

{#if selectedId != null}
	<BrainDecisionDetailDrawer decisionId={selectedId} onClose={closeDrawer} />
{/if}
