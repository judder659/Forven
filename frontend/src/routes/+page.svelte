<script lang="ts">
	import { onMount, onDestroy } from "svelte";
	import {
		getDashboardOverview,
		getDashboardActivity,
		getDashboardWinners,
	} from "$lib/api";
	import type {
		DashboardOverview,
		DashboardActivityItem,
		WinnerEntry,
	} from "$lib/api";
	import { backendConnected } from "$lib/stores";
	import {
		createRealtimeRefresh,
		type RealtimeRefreshController,
	} from "$lib/utils/realtime";
	import OpsHeaderStrip from "$lib/components/dashboard/OpsHeaderStrip.svelte";
	import SystemPulsePanel from "$lib/components/dashboard/SystemPulsePanel.svelte";
	import DataIntegrityPanel from "$lib/components/dashboard/DataIntegrityPanel.svelte";
	import AlertsFeed from "$lib/components/dashboard/AlertsFeed.svelte";
	import SchedulerWatchPanel from "$lib/components/dashboard/SchedulerWatchPanel.svelte";
	import PaperSessionSummary from "$lib/components/dashboard/PaperSessionSummary.svelte";
	import PipelineFlowPanel from "$lib/components/dashboard/PipelineFlowPanel.svelte";
	import AgentHeartbeat from "$lib/components/dashboard/AgentHeartbeat.svelte";
	import ActivityStream from "$lib/components/dashboard/ActivityStream.svelte";
	import StrategyLeaderboard from "$lib/components/dashboard/StrategyLeaderboard.svelte";
	import EquityOverlay from "$lib/components/dashboard/EquityOverlay.svelte";
	import Skeleton from "$lib/components/Skeleton.svelte";
	import LiveTradingPanel from "$lib/components/dashboard/LiveTradingPanel.svelte";
	import CriticalAlertsBanner from "$lib/components/dashboard/CriticalAlertsBanner.svelte";
	import CrucibleResearchPanel from "$lib/components/dashboard/CrucibleResearchPanel.svelte";

	/** Loader data from +page.ts — provides initial dashboard payload. */
	export let data: {
		overview: DashboardOverview | null;
		activity: DashboardActivityItem[];
		winners: WinnerEntry[];
	};

	// Seed local state from loader data so the page renders on frame 1.
	let overview: DashboardOverview | null = data.overview;
	let activity: DashboardActivityItem[] = data.activity;
	let winners: WinnerEntry[] = data.winners;

	let loadingError = "";
	let loading = !data.overview;
	let primaryRealtime: RealtimeRefreshController | null = null;
	let primaryLoadingInFlight = false;
	let primaryDashboardLoaded = !!data.overview;
	const DASHBOARD_TIMEOUT_MS = 8_000;

	// Persisted activity-stream expand/collapse (full firehose; alerts have
	// their own always-visible feed).
	const ACTIVITY_KEY = "dashboard.activityStream.expanded";
	let activityExpanded = false;

	function toggleActivity() {
		activityExpanded = !activityExpanded;
		try {
			localStorage.setItem(ACTIVITY_KEY, String(activityExpanded));
		} catch {
			// localStorage may be unavailable (private mode / SSR harness); ignore.
		}
	}

	function withTimeout<T>(
		promise: Promise<T>,
		label: string,
		timeoutMs = DASHBOARD_TIMEOUT_MS,
	): Promise<T> {
		return new Promise<T>((resolve, reject) => {
			const timer = setTimeout(
				() => reject(new Error(`${label} timed out`)),
				timeoutMs,
			);
			promise.then(
				(value) => {
					clearTimeout(timer);
					resolve(value);
				},
				(err) => {
					clearTimeout(timer);
					reject(err);
				},
			);
		});
	}

	async function loadDashboard() {
		if (primaryLoadingInFlight) return;
		primaryLoadingInFlight = true;

		try {
			const results = await Promise.allSettled([
				withTimeout(getDashboardOverview(), "overview"),
				withTimeout(getDashboardActivity(40), "activity"),
				withTimeout(getDashboardWinners(10), "winners"),
			]);
			const [overviewResult, activityResult, winnersResult] = results;

			if (overviewResult.status === "fulfilled") {
				overview = overviewResult.value;
				loadingError = "";
			}
			if (activityResult.status === "fulfilled")
				activity = activityResult.value;
			if (winnersResult.status === "fulfilled")
				winners = winnersResult.value;

			// An always-on dashboard must not die on a transient miss: keep the
			// last good data on screen and only surface an error when we have
			// nothing at all to show. The ops header independently shows
			// backend reachability, so a stale-but-rendered dashboard is
			// visibly distinguishable from a healthy one.
			const allFailed = results.every((entry) => entry.status === "rejected");
			if (allFailed && !overview) {
				loadingError = $backendConnected
					? "Dashboard data is temporarily unavailable. Retrying in background."
					: "Backend connection is still initializing. Dashboard will auto-retry.";
			}

			loading = false;
			primaryDashboardLoaded = true;
		} finally {
			primaryLoadingInFlight = false;
		}
	}

	function startPrimaryRealtime() {
		if (primaryRealtime) return;
		primaryRealtime = createRealtimeRefresh(loadDashboard, {
			fallbackMs: 30_000,
			wsDebounceMs: 5000,
			wsEvents: [
				"strategy_promoted",
				"kill_switch_activated",
				"kill_switch_cleared",
				"agent_stalled",
			],
			pollWhenWsOfflineOnly: false,
		});
		primaryRealtime.start();
	}

	function stopPrimaryRealtime() {
		primaryRealtime?.stop();
		primaryRealtime = null;
	}

	onMount(() => {
		try {
			activityExpanded = localStorage.getItem(ACTIVITY_KEY) === "true";
		} catch {
			activityExpanded = false;
		}

		if (!primaryDashboardLoaded) {
			loading = true;
			void loadDashboard();
		}
		startPrimaryRealtime();
	});

	onDestroy(() => {
		stopPrimaryRealtime();
	});
</script>

<svelte:head>
	<title>Operations | Forven</title>
	<meta
		name="description"
		content="Always-on operations dashboard: system health, data integrity, pipeline flow, paper trading, and alerts."
	/>
</svelte:head>

<div
	class="relative flex h-full min-h-0 flex-col gap-4 overflow-hidden bg-black px-4 py-6"
>
	<CriticalAlertsBanner />

	<div class="flex-shrink-0 border-b border-[#222] pb-4">
		<h1 class="text-lg font-bold uppercase tracking-widest text-white">Dashboard</h1>
		<p class="mt-1 text-xs text-[#666]">
			Live operations, trading exposure, agent activity, and strategy pipeline health.
		</p>
	</div>

	<OpsHeaderStrip autopilot={overview?.autopilot ?? null} kpis={overview?.kpis ?? null} />

	{#if loading && !overview}
		<div class="min-h-[220px] grid grid-cols-[1fr_1fr_1fr] gap-2">
			<div class="border border-[#222] p-4 bg-[#050505]"><Skeleton rows={6} /></div>
			<div class="border border-[#222] p-4 bg-[#050505]"><Skeleton rows={6} /></div>
			<div class="border border-[#222] p-4 bg-[#050505]"><Skeleton rows={6} /></div>
		</div>
		<div class="min-h-[240px] grid grid-cols-2 gap-2">
			<div class="border border-[#222] p-4 bg-[#050505]"><Skeleton rows={8} /></div>
			<div class="border border-[#222] p-4 bg-[#050505]"><Skeleton rows={8} /></div>
		</div>
	{:else}
		<div class="flex-1 min-h-0 overflow-y-auto overflow-x-hidden">
			<div class="space-y-2 pb-2">
				<!-- Monitor row: is the machine alive, is the data trustworthy, what needs attention -->
				<div class="grid grid-cols-1 gap-2 lg:h-[330px] lg:grid-cols-3">
					<div class="flex min-h-0 flex-col gap-2">
						<div class="min-h-0 flex-1"><SystemPulsePanel /></div>
						<div class="min-h-0 flex-1"><DataIntegrityPanel /></div>
					</div>
					<div class="h-[240px] min-h-0 lg:h-auto"><AlertsFeed /></div>
					<div class="h-[240px] min-h-0 lg:h-auto"><SchedulerWatchPanel /></div>
				</div>

				<!-- Trading row: what is the money doing right now -->
				<div class="flex-shrink-0 space-y-2">
					<LiveTradingPanel />
				</div>
				<div class="flex-shrink-0">
					<PaperSessionSummary />
				</div>

				<!-- Agent activity + pipeline flow, side by side -->
				<div class="flex-shrink-0 grid grid-cols-1 gap-2 lg:grid-cols-2 lg:h-[260px]">
					<div class="h-[240px] min-h-0 lg:h-auto"><AgentHeartbeat /></div>
					<div class="h-[240px] min-h-0 lg:h-auto"><PipelineFlowPanel /></div>
				</div>

				<!-- Research: active crucibles + recent verdicts -->
				<div class="flex-shrink-0 h-[180px]">
					<CrucibleResearchPanel />
				</div>

				<div
					class="flex-shrink-0 min-h-[180px] overflow-hidden terminal-card p-1.5"
				>
					<EquityOverlay />
				</div>

				<div class="flex-shrink-0 h-[230px]">
					<StrategyLeaderboard {winners} />
				</div>

				<!-- Full activity firehose (alerts have their own panel above) -->
				<div class="flex-shrink-0 terminal-card">
					<button
						type="button"
						class="w-full text-left px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888] hover:text-white transition-colors"
						on:click={toggleActivity}
						aria-expanded={activityExpanded}
						aria-controls="activity-stream-panel"
						data-testid="activity-stream-toggle"
					>
						Activity Stream {activityExpanded ? "▾" : "▸"}
					</button>
					{#if activityExpanded}
						<div id="activity-stream-panel" class="max-h-[280px] overflow-auto">
							<ActivityStream items={activity} />
						</div>
					{/if}
				</div>
			</div>
		</div>
	{/if}

	{#if loadingError}
		<div
			class="flex-shrink-0 border border-red-900 bg-red-500/5 px-4 py-2 text-xs text-red-400"
		>
			{loadingError}
		</div>
	{/if}
</div>
