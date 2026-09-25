<script lang="ts">
	/**
	 * Live-first dashboard: is real money trading safely, what is it doing, are
	 * the live strategies actually firing, and what needs the operator. Paper,
	 * pipeline and system health get one line each at the bottom.
	 *
	 * Account, risk and open trades live in the shared stores the header
	 * heartbeat also fills; this page refreshes them on its own cadence so the
	 * numbers never wait on a quiet websocket.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		getForvenDashboard,
		getForvenEquityHistory,
		getForvenOpenTrades,
		getForvenRisk,
	} from '$lib/api';
	import type { ForvenEquityHistory } from '$lib/api';
	import {
		getDashboardFunnel,
		getLiveFleet,
		getPaperSummary,
		getSchedulerJobs,
		getTaskHealth,
	} from '$lib/api/dashboard';
	import type {
		DashboardFunnelStage,
		LiveFleet,
		PaperSummary,
		SchedulerJobSummary,
		TaskHealth,
	} from '$lib/api/dashboard';
	import { forvenDashboard, forvenOpenTrades, forvenRisk } from '$lib/stores/forven';
	import { forvenLivePrices } from '$lib/stores/forvenWebSocket';
	import { navRouteMetrics } from '$lib/stores/navMetrics';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import { buildAttentionItems, formatAge, isLiveTrade } from '$lib/utils/liveDashboard';
	import CriticalAlertsBanner from '$lib/components/dashboard/CriticalAlertsBanner.svelte';
	import LiveStatusBar from '$lib/components/dashboard/LiveStatusBar.svelte';
	import AccountTiles from '$lib/components/dashboard/AccountTiles.svelte';
	import LivePositions from '$lib/components/dashboard/LivePositions.svelte';
	import AttentionPanel from '$lib/components/dashboard/AttentionPanel.svelte';
	import LiveStrategiesTable from '$lib/components/dashboard/LiveStrategiesTable.svelte';
	import LivePnlPanel from '$lib/components/dashboard/LivePnlPanel.svelte';
	import RecentFills from '$lib/components/dashboard/RecentFills.svelte';
	import WalletCapacity from '$lib/components/dashboard/WalletCapacity.svelte';
	import OpsFooter from '$lib/components/dashboard/OpsFooter.svelte';

	export let data: { fleet: LiveFleet | null };

	let fleet: LiveFleet | null = data.fleet;
	let fleetUnavailable = false;
	let equityHistory: ForvenEquityHistory | null = null;
	let schedulerJobs: SchedulerJobSummary[] = [];
	let paper: PaperSummary | null = null;
	let funnel: DashboardFunnelStage[] = [];
	let health: TaskHealth | null = null;
	let healthUnreachable = false;
	let refreshedAt: number | null = null;
	let now = Date.now();
	let realtime: RealtimeRefreshController | null = null;
	let clock: ReturnType<typeof setInterval> | null = null;

	async function refresh(): Promise<void> {
		const [dash, risk, open, fleetResult, equity, jobs, paperResult, funnelResult, healthResult] =
			await Promise.allSettled([
				getForvenDashboard(),
				getForvenRisk(),
				getForvenOpenTrades(),
				getLiveFleet(),
				getForvenEquityHistory(),
				getSchedulerJobs(),
				getPaperSummary(false),
				getDashboardFunnel(),
				getTaskHealth(),
			]);
		if (dash.status === 'fulfilled' && dash.value) forvenDashboard.set(dash.value);
		if (risk.status === 'fulfilled' && risk.value) forvenRisk.set(risk.value);
		if (open.status === 'fulfilled' && Array.isArray(open.value)) forvenOpenTrades.set(open.value);
		// Keep the last good fleet on screen through a transient miss.
		if (fleetResult.status === 'fulfilled') fleet = fleetResult.value;
		fleetUnavailable = fleetResult.status === 'rejected' && fleet === null;
		if (equity.status === 'fulfilled') equityHistory = equity.value;
		if (jobs.status === 'fulfilled') schedulerJobs = jobs.value;
		if (paperResult.status === 'fulfilled') paper = paperResult.value;
		if (funnelResult.status === 'fulfilled') funnel = funnelResult.value;
		if (healthResult.status === 'fulfilled') health = healthResult.value;
		healthUnreachable = healthResult.status === 'rejected';
		refreshedAt = Date.now();
	}

	$: dashboard = $forvenDashboard;
	$: risk = $forvenRisk;
	$: liveTrades = $forvenOpenTrades.filter(isLiveTrade);
	$: prices = { ...(dashboard?.prices ?? {}), ...$forvenLivePrices };
	// null = unknown (e.g. an older backend without the live counts): never claim
	// "paper only" without evidence.
	$: armedCount = fleet
		? fleet.strategies.length + fleet.live_bots_armed
		: typeof dashboard?.live_strategy_count === 'number'
			? dashboard.live_strategy_count + (dashboard.live_bot_count ?? 0)
			: null;
	$: hasLive = armedCount === null || armedCount > 0 || liveTrades.length > 0;
	$: attention = buildAttentionItems({
		dashboard,
		risk,
		fleet,
		schedulerJobs,
		pendingApprovals: $navRouteMetrics['/approval']?.count ?? 0,
		now,
	});

	onMount(() => {
		void refresh();
		realtime = createRealtimeRefresh(refresh, {
			fallbackMs: 30_000,
			wsDebounceMs: 2_000,
			wsEvents: ['trade', 'kill_switch_activated', 'kill_switch_cleared', 'strategy_promoted', 'strategy_transition', 'risk_alert'],
			pollWhenWsOfflineOnly: false,
		});
		realtime.start();
		clock = setInterval(() => (now = Date.now()), 5_000);
	});

	onDestroy(() => {
		realtime?.stop();
		realtime = null;
		if (clock) clearInterval(clock);
	});
</script>

<svelte:head>
	<title>Dashboard | Forven</title>
	<meta
		name="description"
		content="Live trading first: account, positions, live strategy health, realized P&L, and what needs attention."
	/>
</svelte:head>

<div class="flex h-full min-h-0 flex-col overflow-hidden bg-black">
	<div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-4 py-4">
		<div class="mx-auto max-w-[1600px] space-y-3">
			<CriticalAlertsBanner />

			<div class="flex items-baseline justify-between">
				<h1 class="text-lg font-bold uppercase tracking-widest text-white">Dashboard</h1>
				<span class="text-[10px] uppercase tracking-wider text-gray-600">
					{refreshedAt ? `updated ${formatAge(new Date(refreshedAt).toISOString(), now)} ago` : 'loading…'}
				</span>
			</div>

			<LiveStatusBar {dashboard} {risk} {fleet} {now} />

			{#if hasLive}
				<AccountTiles {dashboard} {risk} {fleet} />

				<div class="grid gap-3 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
					<LivePositions
						trades={liveTrades}
						{prices}
						budgetPositions={risk?.portfolio_budget_live?.positions ?? []}
						{now}
					/>
					<AttentionPanel items={attention} />
				</div>

				{#if fleetUnavailable}
					<div class="border border-amber-900 bg-amber-500/5 px-3 py-2 text-xs text-amber-300">
						Live strategy data is unavailable right now. Retrying.
					</div>
				{:else}
					<LiveStrategiesTable
						strategies={fleet?.strategies ?? []}
						sizing={risk?.portfolio_budget_live?.strategy_sizing ?? []}
						liveBotsArmed={fleet?.live_bots_armed ?? 0}
						{now}
					/>
				{/if}

				<div class="grid gap-3 xl:grid-cols-2">
					<LivePnlPanel history={equityHistory} />
					<RecentFills fills={fleet?.recent_fills ?? []} {now} />
				</div>

				<WalletCapacity budget={risk?.portfolio_budget_live ?? null} capacity={fleet?.capacity ?? null} />
			{:else}
				<div class="grid gap-3 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
					<div class="border border-[#222] bg-[#050505] px-4 py-5 text-xs text-gray-400" data-testid="no-live">
						<div class="text-sm font-bold text-gray-200">No strategies are trading real money.</div>
						<p class="mt-2 max-w-prose">
							A strategy trades real money once it is promoted from paper to live. Paper results and go-live
							candidates are on the pages below.
						</p>
						<div class="mt-3 flex gap-3 text-[11px] uppercase tracking-wider">
							<a href="/paper-trades" class="text-gray-300 hover:text-white">Paper trades →</a>
							<a href="/approval" class="text-gray-300 hover:text-white">Approvals →</a>
						</div>
					</div>
					<AttentionPanel items={attention} />
				</div>
			{/if}

			<OpsFooter {paper} {funnel} {health} {healthUnreachable} />
		</div>
	</div>
</div>
