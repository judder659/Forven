<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
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

	function outcomeClass(value: string | null): string {
		switch ((value ?? '').toLowerCase()) {
			case 'success':
				return 'chip chip-success';
			case 'failure':
				return 'chip chip-failure';
			case 'mixed':
				return 'chip chip-mixed';
			default:
				return 'chip chip-pending';
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

<div class="decisions-tab">
	<div class="filters">
		<label>
			<span>Cycle ID</span>
			<input
				type="text"
				bind:value={cycleId}
				placeholder="e.g. cycle-2026-04-25-T03"
			/>
		</label>
		<label>
			<span>Action type</span>
			<input
				type="text"
				bind:value={actionType}
				placeholder="research, backtest, paper, …"
			/>
		</label>
		<label>
			<span>Strategy ID</span>
			<input
				type="text"
				bind:value={strategyId}
				placeholder="e.g. S00123"
			/>
		</label>
		<label>
			<span>Outcome</span>
			<select bind:value={outcome}>
				<option value="">All</option>
				<option value="success">Success</option>
				<option value="failure">Failure</option>
				<option value="mixed">Mixed</option>
			</select>
		</label>
		<div class="filter-actions">
			<button type="button" class="primary" on:click={applyFilters} disabled={loading}>
				Apply
			</button>
			<button type="button" on:click={clearFilters} disabled={loading}>Clear</button>
		</div>
	</div>

	{#if error}
		<div class="error-banner">
			<strong>Failed to load decisions:</strong>
			{error}
			<button class="link-btn" type="button" on:click={() => loadFirstPage()}>retry</button>
		</div>
	{/if}

	{#if loading && items.length === 0}
		<div class="loading">Loading decisions…</div>
	{:else if items.length === 0 && !error}
		<div class="empty">
			No decisions yet — Brain hasn't run with the new logging in place.
		</div>
	{:else}
		<div class="meta">{items.length} of {total} decisions</div>
		<ul class="rows">
			{#each items as row (row.id)}
				<li>
					<button type="button" class="row" on:click={() => openDrawer(row.id)}>
						<div class="row-head">
							<span class="cycle">{row.cycle_id ?? '—'}</span>
							<span class="action chip">{actionLabel(row)}</span>
							<span class={outcomeClass(row.outcome_observed)}>
								{row.outcome_observed ?? 'pending'}
							</span>
							<span class="when">{formatTimestamp(row.created_at)}</span>
						</div>
						<div class="row-summary">{previewSituation(row.situation_summary)}</div>
					</button>
				</li>
			{/each}
		</ul>

		{#if items.length < total}
			<div class="load-more">
				<button type="button" on:click={loadMore} disabled={loading}>
					{loading ? 'Loading…' : `Load more (${total - items.length} remaining)`}
				</button>
			</div>
		{/if}
	{/if}
</div>

{#if selectedId != null}
	<BrainDecisionDetailDrawer decisionId={selectedId} onClose={closeDrawer} />
{/if}

<style>
	.decisions-tab {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.filters {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 0.75rem;
		align-items: end;
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
		padding: 0.875rem;
	}

	.filters label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.75rem;
		color: #888;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.filters input,
	.filters select {
		background: #050505;
		border: 1px solid #333;
		border-radius: 0;
		color: #e5e5e5;
		padding: 0.4rem 0.5rem;
		font-size: 0.875rem;
	}

	.filters input:focus,
	.filters select:focus {
		outline: none;
		border-color: #fff;
	}

	.filter-actions {
		display: flex;
		gap: 0.5rem;
	}

	.filter-actions button {
		background: #1a1a1a;
		border: 1px solid #333;
		color: #ddd;
		padding: 0.5rem 1rem;
		border-radius: 0;
		cursor: pointer;
		font-size: 0.875rem;
	}

	.filter-actions .primary {
		background: #fff;
		border-color: #fff;
		color: #000;
	}

	.filter-actions .primary:hover:not(:disabled) {
		background: #ddd;
	}

	.filter-actions button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.error-banner {
		background: #2a1010;
		border: 1px solid #5a2020;
		color: #f8c0c0;
		padding: 0.625rem 0.875rem;
		border-radius: 0;
		font-size: 0.875rem;
	}

	.link-btn {
		background: transparent;
		color: #f8c0c0;
		border: none;
		text-decoration: underline;
		cursor: pointer;
		margin-left: 0.5rem;
		padding: 0;
	}

	.loading,
	.empty {
		color: #888;
		font-size: 0.875rem;
		padding: 1rem;
		text-align: center;
		border: 1px dashed #222;
		border-radius: 0;
	}

	.meta {
		color: #888;
		font-size: 0.8125rem;
	}

	.rows {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.row {
		width: 100%;
		text-align: left;
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
		padding: 0.75rem 0.875rem;
		color: #e5e5e5;
		cursor: pointer;
		transition: border-color 120ms ease, background 120ms ease;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.row:hover {
		border-color: #555;
		background: #111;
	}

	.row-head {
		display: flex;
		align-items: center;
		gap: 0.625rem;
		flex-wrap: wrap;
		font-size: 0.8125rem;
	}

	.cycle {
		color: #ccc;
		font-family: 'JetBrains Mono', 'Consolas', monospace;
	}

	.chip {
		padding: 0.125rem 0.5rem;
		border-radius: 0;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		background: #1a1a1a;
		color: #aaa;
		border: 1px solid #222;
	}

	.chip-success {
		background: #14532d;
		color: #86efac;
		border-color: #14532d;
	}

	.chip-failure {
		background: #5b1e1e;
		color: #fca5a5;
		border-color: #5b1e1e;
	}

	.chip-mixed {
		background: #5a4a14;
		color: #fde68a;
		border-color: #5a4a14;
	}

	.chip-pending {
		background: #1a1a1a;
		color: #888;
		border-color: #222;
	}

	.action {
		background: #1a1a1a;
		color: #888;
		border-color: #1a1a1a;
	}

	.when {
		color: #666;
		margin-left: auto;
		font-size: 0.75rem;
	}

	.row-summary {
		color: #ccc;
		font-size: 0.875rem;
		line-height: 1.4;
	}

	.load-more {
		display: flex;
		justify-content: center;
		padding: 0.5rem 0;
	}

	.load-more button {
		background: #1a1a1a;
		border: 1px solid #333;
		color: #ddd;
		padding: 0.5rem 1.25rem;
		border-radius: 0;
		cursor: pointer;
		font-size: 0.875rem;
	}

	.load-more button:hover:not(:disabled) {
		background: #222;
	}

	.load-more button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
