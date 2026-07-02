<script lang="ts">
	/**
	 * Pipeline flow: the funnel as one compact line (counts per stage) plus a
	 * live feed of stage transitions — what moved, where, and why. For an
	 * autonomous factory the motion is the signal; standing counts alone look
	 * identical whether the pipeline is healthy or wedged.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		getDashboardFunnel,
		getRecentLifecycleEvents,
		type DashboardFunnelStage,
		type LifecycleEventSummary,
	} from '$lib/api/dashboard';

	const REFRESH_MS = 30_000;

	let stages: DashboardFunnelStage[] = [];
	let events: LifecycleEventSummary[] = [];
	let loaded = false;
	let error = '';
	let consecutiveFailures = 0;
	let timer: ReturnType<typeof setInterval> | null = null;

	// Canonical funnel order; raw lifecycle states map onto these buckets.
	const BUCKETS: Array<{ key: string; label: string; states: string[] }> = [
		{ key: 'research', label: 'research', states: ['generated', 'quick_screen', 'researching', 'research_only', 'developing'] },
		{ key: 'gauntlet', label: 'gauntlet', states: ['backtesting', 'gauntlet'] },
		{ key: 'paper', label: 'paper', states: ['paper', 'paper_trading'] },
		{ key: 'live', label: 'live', states: ['deployed', 'live_graduated'] },
	];
	const TERMINAL_STATES = new Set(['retired', 'archived', 'rejected', 'backtest_failed']);
	const PROMOTE_STATES = new Set(['backtesting', 'gauntlet', 'paper', 'paper_trading', 'deployed', 'live_graduated']);

	$: countsByState = Object.fromEntries(stages.map((stage) => [stage.state, stage.count]));
	$: bucketCounts = BUCKETS.map((bucket) => ({
		...bucket,
		count: bucket.states.reduce((sum, state) => sum + (countsByState[state] ?? 0), 0),
	}));
	$: terminalCount = stages
		.filter((stage) => TERMINAL_STATES.has(stage.state))
		.reduce((sum, stage) => sum + stage.count, 0);

	$: dayAgo = Date.now() - 24 * 3600 * 1000;
	$: promoted24h = events.filter(
		(event) =>
			eventTime(event) >= dayAgo &&
			PROMOTE_STATES.has(event.toState) &&
			event.fromState !== event.toState,
	).length;
	$: culled24h = events.filter(
		(event) => eventTime(event) >= dayAgo && TERMINAL_STATES.has(event.toState),
	).length;

	// Hide same-state churn (blocked-gate re-evaluations) — motion only.
	$: transitions = events.filter((event) => event.fromState !== event.toState).slice(0, 30);

	function eventTime(event: LifecycleEventSummary): number {
		const iso = event.createdAt.includes('T') ? event.createdAt : `${event.createdAt.replace(' ', 'T')}Z`;
		const parsed = Date.parse(iso);
		return Number.isNaN(parsed) ? 0 : parsed;
	}

	function ageLabel(event: LifecycleEventSummary): string {
		const ts = eventTime(event);
		if (!ts) return '';
		const mins = Math.max(0, (Date.now() - ts) / 60_000);
		if (mins < 1) return 'now';
		if (mins < 90) return `${Math.round(mins)}m`;
		if (mins < 48 * 60) return `${(mins / 60).toFixed(1)}h`;
		return `${(mins / 1440).toFixed(1)}d`;
	}

	function toTone(toState: string): string {
		if (toState === 'paper' || toState === 'paper_trading') return 'text-emerald-400';
		if (toState === 'deployed' || toState === 'live_graduated') return 'text-emerald-300';
		if (toState === 'backtesting' || toState === 'gauntlet') return 'text-white';
		if (toState === 'rejected' || toState === 'backtest_failed') return 'text-red-400';
		return 'text-gray-500';
	}

	async function load(): Promise<void> {
		try {
			const [funnelResult, eventsResult] = await Promise.allSettled([
				getDashboardFunnel(),
				getRecentLifecycleEvents(60),
			]);
			if (funnelResult.status === 'fulfilled') stages = funnelResult.value;
			if (eventsResult.status === 'fulfilled') events = eventsResult.value;
			if (funnelResult.status === 'fulfilled' || eventsResult.status === 'fulfilled') {
				loaded = true;
				error = '';
				consecutiveFailures = 0;
			} else {
				throw (funnelResult as PromiseRejectedResult).reason;
			}
		} catch (err) {
			consecutiveFailures += 1;
			if (consecutiveFailures >= 2) {
				error = err instanceof Error ? err.message : 'Pipeline data unavailable.';
			}
		}
	}

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), REFRESH_MS);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<div class="flex h-full min-h-0 flex-col border border-[#222] bg-[#050505]" data-testid="pipeline-flow-panel">
	<div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[#1a1a1a] px-2.5 py-1.5">
		<h2 class="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Pipeline</h2>
		<span class="flex items-center gap-1.5 font-mono text-[11px]">
			{#each bucketCounts as bucket, i}
				{#if i > 0}<span class="text-gray-700">→</span>{/if}
				<span title="{bucket.label}: {bucket.count} strategies">
					<span class="text-gray-500">{bucket.label}</span>
					<span class={bucket.count > 0 ? 'text-gray-200' : 'text-gray-600'}>{bucket.count}</span>
				</span>
			{/each}
			<span class="text-gray-700">·</span>
			<span title="Retired + rejected (graveyard)"><span class="text-gray-500">out</span> <span class="text-gray-400">{terminalCount}</span></span>
		</span>
		<span class="ml-auto font-mono text-[10px] text-gray-500" title="Stage transitions in the last 24h (from the most recent {events.length} events)">
			24h: <span class="text-white">+{promoted24h} advanced</span> · <span class="text-gray-400">−{culled24h} culled</span>
		</span>
	</div>
	<div class="min-h-0 flex-1 overflow-y-auto px-2.5 py-1.5">
		{#if error && !loaded}
			<div class="text-xs text-red-300">{error}</div>
		{:else if !loaded}
			<div class="text-xs text-gray-500">Loading…</div>
		{:else if transitions.length === 0}
			<div class="text-xs text-gray-600">No stage transitions yet.</div>
		{:else}
			<ul class="space-y-0.5 font-mono text-[11px]">
				{#each transitions as event (event.id)}
					<li class="flex items-center gap-2" title={event.reason}>
						<span class="w-8 shrink-0 text-right text-[10px] text-gray-600">{ageLabel(event)}</span>
						<a href="/lab/strategy/{event.strategyId}" class="shrink-0 text-gray-300 hover:text-white hover:underline">{event.strategyId}</a>
						<span class="shrink-0 text-gray-600">{event.fromState}</span>
						<span class="shrink-0 text-gray-700">→</span>
						<span class="shrink-0 {toTone(event.toState)}">{event.toState}</span>
						<span class="min-w-0 flex-1 truncate text-gray-600">{event.reason}</span>
						<span class="shrink-0 text-[10px] text-gray-700">{event.actor}</span>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</div>
