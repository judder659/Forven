<script lang="ts">
	/**
	 * Strategy → Brain decisions side-card (P1-T19).
	 *
	 * Shows up to 5 most recent Brain decisions linked to this strategy via
	 * agent_tasks.strategy_id. Suppressed entirely when there are none, so
	 * manually-created strategies stay clean.
	 *
	 * Calls /api/brain/decisions?strategy_id=<id>&limit=5 and renders cycle_id,
	 * decision JSON preview, outcome chip, and a deep-link to /brain.
	 */
	import { onMount } from 'svelte';
	import {
		getBrainDecisions,
		type BrainDecisionRow,
		type BrainDecisionOutcome,
	} from '$lib/api/brain';

	export let strategyId: string;

	const LIMIT = 5;

	let items: BrainDecisionRow[] = [];
	let total = 0;
	let loaded = false;
	let loading = false;
	let error: string | null = null;

	$: deepLinkHref = strategyId
		? `/brain?tab=decisions&d_strategy=${encodeURIComponent(strategyId)}`
		: '/brain?tab=decisions';

	async function load() {
		if (!strategyId) {
			loaded = true;
			return;
		}
		loading = true;
		error = null;
		try {
			const res = await getBrainDecisions({ strategyId, limit: LIMIT });
			items = res.items ?? [];
			total = res.total ?? items.length;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load decisions';
		} finally {
			loading = false;
			loaded = true;
		}
	}

	onMount(() => {
		void load();
	});

	$: if (strategyId) {
		// Reload if the parent navigates to a different strategy without
		// remounting the component.
		void load();
	}

	function decisionPreview(row: BrainDecisionRow): string {
		const blob = row.decision ?? row.decision_json;
		if (!blob) return row.situation_summary ?? '';
		try {
			const text = typeof blob === 'string' ? blob : JSON.stringify(blob);
			return text.length > 240 ? `${text.slice(0, 240)}…` : text;
		} catch {
			return row.situation_summary ?? '';
		}
	}

	function outcomeClass(outcome: BrainDecisionOutcome | null): string {
		if (outcome === 'success') return 'outcome-success';
		if (outcome === 'failure') return 'outcome-failure';
		if (outcome === 'mixed') return 'outcome-mixed';
		return 'outcome-pending';
	}

	function outcomeLabel(outcome: BrainDecisionOutcome | null): string {
		return outcome ?? 'pending';
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}
</script>

{#if loaded && !error && items.length > 0}
	<aside
		class="card"
		aria-labelledby="brain-decisions-card-heading"
	>
		<header>
			<h3 id="brain-decisions-card-heading">
				Brain decisions about this strategy
				<span class="count" aria-label="{total} total">{total}</span>
			</h3>
			<a class="deep-link" href={deepLinkHref}>View in /brain →</a>
		</header>

		<ul>
			{#each items as row (row.id)}
				<li>
					<div class="row-head">
						{#if row.cycle_id}
							<span class="cycle" title="cycle_id">cycle {row.cycle_id}</span>
						{/if}
						<span class="outcome {outcomeClass(row.outcome_observed)}">
							{outcomeLabel(row.outcome_observed)}
						</span>
						<span class="when">{formatTimestamp(row.created_at)}</span>
					</div>
					<p class="preview">{decisionPreview(row)}</p>
				</li>
			{/each}
		</ul>
	</aside>
{:else if error}
	<aside class="card error" role="alert">
		<p>Failed to load Brain decisions: {error}</p>
		<button type="button" on:click={load}>Retry</button>
	</aside>
{:else if loading && !loaded}
	<aside class="card loading" aria-busy="true">
		<p>Loading Brain decisions…</p>
	</aside>
{/if}

<style>
	.card {
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
		padding: 0.875rem;
		display: flex;
		flex-direction: column;
		gap: 0.625rem;
	}

	.card.error {
		border-color: #5a2020;
		background: #2a1010;
		color: #f8c0c0;
	}

	.card.loading {
		color: #888;
	}

	header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
	}

	header h3 {
		margin: 0;
		font-size: 0.9375rem;
		color: #ddd;
	}

	.count {
		display: inline-block;
		min-width: 1.5em;
		margin-left: 0.375rem;
		padding: 0 0.5rem;
		border-radius: 0;
		background: #1a1a1a;
		color: #888;
		font-size: 0.6875rem;
		font-weight: 600;
		text-align: center;
	}

	.deep-link {
		color: #888;
		text-decoration: none;
		font-size: 0.8125rem;
	}

	.deep-link:hover {
		text-decoration: underline;
	}

	ul {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	li {
		background: #050505;
		border: 1px solid #1a1a1a;
		border-radius: 0;
		padding: 0.5rem 0.625rem;
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}

	.row-head {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		flex-wrap: wrap;
		font-size: 0.75rem;
	}

	.cycle {
		font-family: 'JetBrains Mono', 'Consolas', monospace;
		color: #aaa;
	}

	.outcome {
		padding: 0.0625rem 0.4rem;
		border-radius: 0;
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
	}

	.outcome-success {
		background: #14532d;
		color: #86efac;
	}

	.outcome-failure {
		background: #5a1a1a;
		color: #fca5a5;
	}

	.outcome-mixed {
		background: #4a3814;
		color: #fde68a;
	}

	.outcome-pending {
		background: #1f1f1f;
		color: #888;
	}

	.when {
		margin-left: auto;
		color: #666;
	}

	.preview {
		margin: 0;
		font-size: 0.8125rem;
		color: #888;
		font-family: 'JetBrains Mono', 'Consolas', monospace;
		line-height: 1.45;
		white-space: pre-wrap;
		word-wrap: break-word;
	}

	button {
		align-self: flex-start;
		margin-top: 0.25rem;
		background: transparent;
		color: #f8c0c0;
		border: 1px solid #5a2020;
		border-radius: 0;
		padding: 0.25rem 0.625rem;
		cursor: pointer;
		font-size: 0.75rem;
	}
</style>
