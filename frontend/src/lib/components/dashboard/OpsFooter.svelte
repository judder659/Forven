<script lang="ts">
	/** Paper trading, the research pipeline and system health — one line each, with links. */
	import type { DashboardFunnelStage, PaperSummary, TaskHealth } from '$lib/api/dashboard';
	import { formatUsd, pnlTone } from '$lib/utils/liveDashboard';

	export let paper: PaperSummary | null = null;
	export let funnel: DashboardFunnelStage[] = [];
	export let health: TaskHealth | null = null;
	/** True when the last health check failed outright. */
	export let healthUnreachable = false;

	const BUCKETS: Array<{ label: string; states: string[] }> = [
		{ label: 'research', states: ['generated', 'quick_screen', 'researching', 'developing'] },
		{ label: 'gauntlet', states: ['backtesting', 'gauntlet'] },
		{ label: 'paper', states: ['paper', 'paper_trading'] },
		{ label: 'live', states: ['deployed', 'live_graduated'] },
	];

	$: counts = Object.fromEntries(funnel.map((stage) => [stage.state, stage.count]));
	$: pipeline = BUCKETS.map((bucket) => ({
		label: bucket.label,
		count: bucket.states.reduce((sum, state) => sum + (counts[state] ?? 0), 0),
	}));
	$: healthy = !healthUnreachable && health?.status === 'ok';
</script>

<div class="grid gap-2 text-[11px] md:grid-cols-3" data-testid="ops-footer">
	<a href="/paper-trades" class="border border-[#222] bg-[#050505] px-3 py-2 hover:border-[#444]">
		<div class="text-[10px] uppercase tracking-wider text-gray-500">Paper trading</div>
		{#if paper}
			<div class="mt-0.5 text-gray-300">
				{paper.totals.session_count} strategies · {paper.totals.open_count} open ·
				<span title="Realized across current paper sessions" class="font-mono {pnlTone(paper.totals.realized_pnl_usd)}">
					{formatUsd(paper.totals.realized_pnl_usd, true)}
				</span>
			</div>
		{:else}
			<div class="mt-0.5 text-gray-500">—</div>
		{/if}
	</a>
	<a href="/pipeline" class="border border-[#222] bg-[#050505] px-3 py-2 hover:border-[#444]">
		<div class="text-[10px] uppercase tracking-wider text-gray-500">Pipeline</div>
		<div class="mt-0.5 text-gray-300">
			{#each pipeline as bucket, index (bucket.label)}
				{#if index > 0}<span class="mx-1 text-gray-600">→</span>{/if}<span class="text-gray-500">{bucket.label}</span>
				<span class="font-mono">{bucket.count}</span>
			{/each}
		</div>
	</a>
	<a href="/diagnostics" class="border border-[#222] bg-[#050505] px-3 py-2 hover:border-[#444]">
		<div class="text-[10px] uppercase tracking-wider text-gray-500">System</div>
		<div class="mt-0.5 flex items-center gap-1.5 text-gray-300">
			<span class="h-2 w-2 rounded-full {healthUnreachable ? 'bg-red-500' : healthy ? 'bg-emerald-500' : health ? 'bg-amber-400' : 'bg-[#444]'}"></span>
			{#if healthUnreachable}
				Backend unreachable
			{:else if !health}
				Checking…
			{:else if healthy}
				Healthy
			{:else}
				<span class="truncate" title={health.issues.join('\n')}>Degraded{health.issues[0] ? `: ${health.issues[0]}` : ''}</span>
			{/if}
		</div>
	</a>
</div>
