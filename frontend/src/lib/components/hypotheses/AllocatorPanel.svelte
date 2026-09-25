<script lang="ts">
	import { onMount } from 'svelte';
	import { getAllocatorOverview, type AllocatorOverview } from '$lib/api/hypotheses';

	/** How many ranked rows to show before "show all". */
	const COLLAPSED_ROWS = 10;
	const EXPANDED_KEY = 'forven:crucibles:allocator:expanded';

	let overview: AllocatorOverview | null = null;
	let error = '';
	let loading = true;
	let expanded = false;
	let showAll = false;

	export async function refresh(): Promise<void> {
		try {
			overview = await getAllocatorOverview(40);
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Allocator overview unavailable.';
		} finally {
			loading = false;
		}
	}

	function toggleExpanded(): void {
		expanded = !expanded;
		try {
			localStorage.setItem(EXPANDED_KEY, expanded ? '1' : '0');
		} catch {
			/* non-persistent */
		}
	}

	onMount(() => {
		try {
			expanded = localStorage.getItem(EXPANDED_KEY) === '1';
		} catch {
			/* default collapsed */
		}
		void refresh();
	});

	$: budget = overview?.budget;
	$: quota = overview?.short_quota;
	$: dataQuota = overview?.data_quota;
	$: pool = overview?.pool;
	$: yieldSummary = overview?.yield;
	$: rows = overview?.crucibles ?? [];
	$: visibleRows = showAll ? rows : rows.slice(0, COLLAPSED_ROWS);
	$: quotaOnTrack = quota ? quota.develops_today === 0 || quota.share_pct >= quota.target_pct : true;
	$: dataQuotaOnTrack = dataQuota
		? dataQuota.develops_today === 0 || dataQuota.share_pct >= dataQuota.target_pct
		: true;

	function scoreTone(score: number): string {
		if (score >= 6) return 'text-emerald-400';
		if (score >= 2) return 'text-white';
		if (score >= 0) return 'text-[#888]';
		return 'text-red-400';
	}

	function statusChip(status: string): string {
		if (status === 'proven') return 'border-emerald-800 text-emerald-400';
		if (status === 'researching') return 'border-[#333] text-[#888]';
		return 'border-[#333] text-[#666]';
	}
</script>

<div class="border border-[#222] bg-[#050505]" data-testid="allocator-panel">
	<!-- Single-line summary header; the ranked table lives behind the chevron. -->
	<button
		type="button"
		class="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 text-left hover:bg-[#0a0a0a]"
		aria-expanded={expanded}
		on:click={toggleExpanded}
		title="CRUX-1: both research loops rank the pool by expected survivor value and share this daily develop budget. Click for the ranked pool."
	>
		<span class="text-[10px] font-bold uppercase tracking-wider text-[#888]">
			{expanded ? '▾' : '▸'} Research Budget
		</span>
		{#if loading}
			<span class="text-[11px] text-[#555]">loading…</span>
		{:else if error}
			<span class="text-[11px] text-[#555]">{error}</span>
		{:else if overview}
			<span class="text-[11px] text-[#888]">
				<span class="font-bold text-white">{budget?.used_today ?? 0}</span>/{budget?.daily ?? 0} develops today
			</span>
			<span class="text-[11px] {quotaOnTrack ? 'text-emerald-400' : 'text-yellow-500'}">
				short {quota?.share_pct ?? 0}%<span class="text-[#555]">/{quota?.target_pct ?? 0}%</span>
			</span>
			{#if dataQuota}
				<span class="text-[11px] {dataQuotaOnTrack ? 'text-emerald-400' : 'text-yellow-500'}">
					data {dataQuota.share_pct ?? 0}%<span class="text-[#555]">/{dataQuota.target_pct ?? 0}%</span>
				</span>
			{/if}
			<span class="text-[11px] text-[#666]">
				{pool?.total ?? 0} active · {pool?.by_status?.['proven'] ?? 0} proven ·
				<span class={(pool?.with_survivors ?? 0) > 0 ? 'text-emerald-400' : ''}>{pool?.with_survivors ?? 0} with survivors</span>
			</span>
			{#if yieldSummary}
				<span
					class="text-[11px] text-[#666]"
					data-testid="allocator-yield"
					title="Crucible candidates created in the window: untestable = archived before a fair test (no trades at registration, too little feed history, broken code); never traded = own-timeframe backtest with zero trades. Disproven needs fairly-tested children; parked = no attempt could be implemented."
				>
					{yieldSummary.days}d: {yieldSummary.candidates} candidates ·
					<span class={yieldSummary.no_trades + yieldSummary.untestable > 0 ? 'text-yellow-500' : ''}
						>{yieldSummary.untestable} untestable, {yieldSummary.no_trades} never traded</span
					>
					· {yieldSummary.reached_gauntlet} gauntlet ·
					<span class={yieldSummary.reached_paper > 0 ? 'text-emerald-400' : ''}>{yieldSummary.reached_paper} paper</span>
					· {yieldSummary.disproven} disproven, {yieldSummary.parked} parked
				</span>
			{/if}
		{/if}
	</button>

	{#if expanded && overview && !error}
		<div class="overflow-x-auto border-t border-[#1a1a1a]">
			<table class="w-full text-[11px]">
				<thead>
					<tr class="border-b border-[#1a1a1a] text-left text-[9px] uppercase tracking-wider text-[#555]">
						<th class="px-3 py-1">#</th>
						<th class="px-2 py-1">Crucible</th>
						<th class="px-2 py-1">Status</th>
						<th class="px-2 py-1">Family</th>
						<th class="px-2 py-1 text-right" title="CRUX-1 value score: survivors x6, gauntlet x1.5, verdict-positives x2, family prior, minus fruitless/failed develops">Score</th>
						<th class="px-2 py-1 text-right" title="strategies spawned">Kids</th>
						<th class="px-2 py-1 text-right" title="children currently in the gauntlet">Gaunt</th>
						<th class="px-2 py-1 text-right" title="children that reached paper/live — ground truth">Surv</th>
						<th class="px-2 py-1 text-right" title="fruitless + failed develop attempts (3 parks the crucible)">Strikes</th>
					</tr>
				</thead>
				<tbody>
					{#each visibleRows as crucible, index (crucible.id)}
						<tr class="border-b border-[#111] hover:bg-[#0d0d0d] {crucible.survivor_children > 0 ? 'shadow-[inset_2px_0_0_0_rgba(16,185,129,0.7)]' : ''}">
							<td class="px-3 py-1 text-[#555]">{index + 1}</td>
							<td class="max-w-[380px] px-2 py-1">
								<a href={`/hypotheses/${crucible.id}`} class="font-mono text-white hover:underline">{crucible.display_id}</a>
								<span class="ml-2 truncate text-[#666]" title={crucible.title}>{crucible.title.slice(0, 60)}</span>
							</td>
							<td class="px-2 py-1">
								<span class={`border px-1 py-0.5 text-[9px] uppercase tracking-wider ${statusChip(crucible.status)}`}>
									{crucible.status}
								</span>
							</td>
							<td class="px-2 py-1 text-[#888]">{crucible.family}</td>
							<td class={`px-2 py-1 text-right font-bold ${scoreTone(crucible.score)}`}>{crucible.score.toFixed(2)}</td>
							<td class="px-2 py-1 text-right text-[#888]">{crucible.children}</td>
							<td class="px-2 py-1 text-right text-[#888]">{crucible.gauntlet_children || '—'}</td>
							<td class="px-2 py-1 text-right {crucible.survivor_children > 0 ? 'font-bold text-emerald-400' : 'text-[#555]'}">
								{crucible.survivor_children || '—'}
							</td>
							<td class="px-2 py-1 text-right {crucible.fruitless_develops + crucible.failed_develops >= 2 ? 'text-yellow-500' : 'text-[#555]'}">
								{crucible.fruitless_develops + crucible.failed_develops || '—'}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		{#if rows.length > COLLAPSED_ROWS}
			<button
				class="w-full border-t border-[#1a1a1a] px-4 py-1 text-[10px] uppercase tracking-wider text-[#666] hover:text-white"
				on:click={() => (showAll = !showAll)}
			>
				{showAll ? `Show top ${COLLAPSED_ROWS}` : `Show all ${rows.length} ranked`}
			</button>
		{/if}
		<p class="border-t border-[#1a1a1a] px-3 py-1 text-[10px] text-[#555]">
			Dispatch order for both research loops. Survivors dominate the score; green-edged rows are proven earners being exploited.
		</p>
	{/if}
</div>
