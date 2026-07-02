<script lang="ts">
	/**
	 * Crucible research: the idea side of the factory. Active hypotheses (what
	 * the system is currently trying to prove) beside recent verdicts (what it
	 * decided and shelved) — every idea grounded in data, visible at a glance.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		getHypotheses,
		getHypothesisCounts,
		type HypothesisCounts,
		type HypothesisSummary,
	} from '$lib/api/hypotheses';

	const REFRESH_MS = 60_000;

	let active: HypothesisSummary[] = [];
	let recentArchived: HypothesisSummary[] = [];
	let counts: HypothesisCounts | null = null;
	let loaded = false;
	let error = '';
	let consecutiveFailures = 0;
	let timer: ReturnType<typeof setInterval> | null = null;

	async function load(): Promise<void> {
		try {
			const [countsResult, activeResult, archivedResult] = await Promise.allSettled([
				getHypothesisCounts(),
				getHypotheses({ sort: 'updated_desc', limit: 8 }),
				getHypotheses({ view: 'archived', sort: 'updated_desc', limit: 8, include_disproven: true }),
			]);
			if (countsResult.status === 'fulfilled') counts = countsResult.value.counts;
			if (activeResult.status === 'fulfilled') active = activeResult.value.hypotheses ?? [];
			if (archivedResult.status === 'fulfilled') recentArchived = archivedResult.value.hypotheses ?? [];
			if (countsResult.status === 'fulfilled' || activeResult.status === 'fulfilled') {
				loaded = true;
				error = '';
				consecutiveFailures = 0;
			} else {
				throw (countsResult as PromiseRejectedResult).reason;
			}
		} catch (err) {
			consecutiveFailures += 1;
			if (consecutiveFailures >= 2) {
				error = err instanceof Error ? err.message : 'Hypotheses unavailable.';
			}
		}
	}

	function statusTone(status: string): string {
		const normalized = status.toLowerCase();
		if (normalized.includes('confirm') || normalized.includes('graduat') || normalized.includes('proven')) return 'text-emerald-400';
		if (normalized.includes('disprov') || normalized.includes('reject')) return 'text-red-400';
		if (normalized.includes('testing') || normalized.includes('research')) return 'text-white';
		return 'text-[#666]';
	}

	function ageLabel(timestamp: string | null | undefined): string {
		if (!timestamp) return '';
		const parsed = Date.parse(timestamp);
		if (Number.isNaN(parsed)) return '';
		const mins = Math.max(0, (Date.now() - parsed) / 60_000);
		if (mins < 90) return `${Math.round(mins)}m`;
		if (mins < 48 * 60) return `${(mins / 60).toFixed(1)}h`;
		return `${(mins / 1440).toFixed(1)}d`;
	}

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), REFRESH_MS);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<div class="terminal-card flex h-full min-h-0 flex-col" data-testid="crucible-research-panel">
	<div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[#1a1a1a] px-4 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Crucibles</h2>
		{#if counts}
			<span class="font-mono text-[11px]">
				<span class="text-[#666]">active</span> <span class="text-white">{counts.active ?? 0}</span>
				<span class="text-[#444]">·</span>
				<span class="text-[#666]">graduated</span> <span class="text-emerald-400">{counts.graduated ?? 0}</span>
				<span class="text-[#444]">·</span>
				<span class="text-[#666]">archived</span> <span class="text-[#888]">{counts.archived ?? 0}</span>
			</span>
		{/if}
		<a href="/hypotheses" class="ml-auto text-[10px] uppercase tracking-wider text-[#555] hover:text-white transition-colors">all hypotheses →</a>
	</div>
	<div class="grid min-h-0 flex-1 grid-cols-1 gap-x-4 overflow-hidden px-4 py-2 lg:grid-cols-2">
		{#if error && !loaded}
			<div class="text-xs text-red-400">{error}</div>
		{:else if !loaded}
			<div class="text-xs uppercase tracking-widest text-[#555]">Loading…</div>
		{:else}
			<div class="min-h-0 overflow-y-auto">
				<div class="mb-1 text-[9px] uppercase tracking-wider text-[#555]">In the crucible</div>
				{#each active as h (h.id)}
					<div class="flex items-center gap-2 font-mono text-[11px] leading-snug" title={h.title}>
						<a href="/hypotheses?focus={h.display_id ?? h.id}" class="shrink-0 text-[#888] hover:text-white hover:underline">{h.display_id ?? h.id}</a>
						<span class="shrink-0 {statusTone(String(h.crucible_status ?? h.status))}">{h.crucible_status ?? h.status}</span>
						<span class="min-w-0 flex-1 truncate text-[#666]">{h.title}</span>
						<span class="shrink-0 text-[#555]" title="Strategies spawned from this hypothesis">{h.strategy_count}s</span>
						<span class="shrink-0 text-[10px] text-[#444]">{ageLabel(h.updated_at)}</span>
					</div>
				{:else}
					<div class="text-xs text-[#555]">No active hypotheses — the ideation loop will replenish.</div>
				{/each}
			</div>
			<div class="min-h-0 overflow-y-auto">
				<div class="mb-1 text-[9px] uppercase tracking-wider text-[#555]">Recent verdicts</div>
				{#each recentArchived as h (h.id)}
					<div class="flex items-center gap-2 font-mono text-[11px] leading-snug" title={h.verdict_memo?.rationale ?? h.title}>
						<a href="/hypotheses?focus={h.display_id ?? h.id}" class="shrink-0 text-[#888] hover:text-white hover:underline">{h.display_id ?? h.id}</a>
						<span class="shrink-0 {statusTone(String(h.status))}">{h.status}</span>
						<span class="min-w-0 flex-1 truncate text-[#555]">{h.title}</span>
						<span class="shrink-0 text-[10px] text-[#444]">{ageLabel(h.updated_at)}</span>
					</div>
				{:else}
					<div class="text-xs text-[#555]">No verdicts yet.</div>
				{/each}
			</div>
		{/if}
	</div>
</div>
