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
	<a href="/paper-trades" class="rounded-md border border-sc-line bg-sc-panel px-3 py-2 hover:border-sc-line2">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Paper trading</div>
		{#if paper}
			<div class="mt-0.5 text-sc-ink2">
				{paper.totals.session_count} strategies · {paper.totals.open_count} open ·
				<span title="Realized across current paper sessions" class="font-mono {pnlTone(paper.totals.realized_pnl_usd)}">
					{formatUsd(paper.totals.realized_pnl_usd, true)}
				</span>
			</div>
		{:else}
			<div class="mt-0.5 text-sc-ink3">—</div>
		{/if}
	</a>
	<a href="/pipeline" class="rounded-md border border-sc-line bg-sc-panel px-3 py-2 hover:border-sc-line2">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Pipeline</div>
		<div class="mt-0.5 text-sc-ink2">
			{#each pipeline as bucket, index (bucket.label)}
				{#if index > 0}<span class="mx-1 text-sc-ink3">→</span>{/if}<span class="text-sc-ink3">{bucket.label}</span>
				<span class="font-mono">{bucket.count}</span>
			{/each}
		</div>
	</a>
	<a href="/diagnostics" class="rounded-md border border-sc-line bg-sc-panel px-3 py-2 hover:border-sc-line2">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">System</div>
		<div class="mt-0.5 flex items-center gap-1.5 text-sc-ink2">
			<span class="h-2 w-2 rounded-full {healthUnreachable ? 'bg-red-500' : healthy ? 'bg-emerald-500' : health ? 'bg-amber-400' : 'bg-sc-line2'}"></span>
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
