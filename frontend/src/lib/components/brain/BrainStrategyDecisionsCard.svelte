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

	const CHIP = 'rounded border px-1.5 py-px font-plex-cond text-[10.5px] font-medium uppercase tracking-[0.06em]';

	function outcomeClass(outcome: BrainDecisionOutcome | null): string {
		if (outcome === 'success') return `${CHIP} border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]`;
		if (outcome === 'failure') return `${CHIP} border-[#e5574f]/40 bg-[#e5574f]/10 text-[#f2956f]`;
		if (outcome === 'mixed') return `${CHIP} border-[#e7b24a]/40 bg-[#e7b24a]/10 text-[#e7b24a]`;
		return `${CHIP} border-sc-line2 bg-sc-raise text-sc-ink2`;
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
		class="flex min-w-0 flex-col gap-2.5 rounded-md border border-sc-line bg-sc-panel p-3.5"
		aria-labelledby="brain-decisions-card-heading"
	>
		<header class="flex flex-wrap items-baseline justify-between gap-2">
			<h3 id="brain-decisions-card-heading" class="m-0 text-[13px] font-medium text-sc-ink">
				Brain decisions about this strategy
				<span
					class="ml-1.5 inline-block min-w-[1.5em] rounded-full bg-sc-raise px-1.5 text-center font-plex-mono text-[10.5px] text-sc-ink2"
					aria-label="{total} total">{total}</span
				>
			</h3>
			<a class="text-[12px] text-sc-ink2 transition-colors hover:text-sc-ink hover:underline" href={deepLinkHref}>View in /brain →</a>
		</header>

		<ul class="m-0 flex list-none flex-col gap-2 p-0">
			{#each items as row (row.id)}
				<li class="flex flex-col gap-1.5 rounded-md border border-sc-line bg-sc-panel2 px-2.5 py-2">
					<div class="flex flex-wrap items-center gap-2 text-[11.5px]">
						{#if row.cycle_id}
							<span class="break-all font-plex-mono text-sc-ink2" title="cycle_id">cycle {row.cycle_id}</span>
						{/if}
						<span class={outcomeClass(row.outcome_observed)}>
							{outcomeLabel(row.outcome_observed)}
						</span>
						<span class="ml-auto text-sc-ink3">{formatTimestamp(row.created_at)}</span>
					</div>
					<p class="m-0 whitespace-pre-wrap break-words font-plex-mono text-[12px] leading-snug text-sc-ink2">{decisionPreview(row)}</p>
				</li>
			{/each}
		</ul>
	</aside>
{:else if error}
	<aside class="flex flex-col gap-2.5 rounded-md border border-[#e5574f]/40 bg-[#e5574f]/10 p-3.5 text-[12.5px] text-[#f2956f]" role="alert">
		<p class="m-0">Failed to load Brain decisions: {error}</p>
		<button
			type="button"
			class="self-start rounded-md border border-[#e5574f]/40 px-2.5 py-1 text-[12px] text-[#f2956f] transition-colors hover:border-[#f2956f]"
			on:click={load}>Retry</button
		>
	</aside>
{:else if loading && !loaded}
	<aside class="rounded-md border border-sc-line bg-sc-panel p-3.5 text-[12.5px] text-sc-ink3" aria-busy="true">
		<p class="m-0">Loading Brain decisions…</p>
	</aside>
{/if}
