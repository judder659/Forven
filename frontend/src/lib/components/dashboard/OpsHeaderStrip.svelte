<script lang="ts">
	/**
	 * Always-on header: one glance answers "is everything okay right now?".
	 * Big status light (backend health), execution mode badge (paper/testnet —
	 * the do-not-be-surprised signal), autopilot state, and a live clock with
	 * the age of the last successful health check so a wedged dashboard is
	 * visibly distinguishable from a healthy one.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getTaskHealth } from '$lib/api/dashboard';
	import { getSettings } from '$lib/api';
	import type { DashboardOverview } from '$lib/api';

	export let autopilot: DashboardOverview['autopilot'] | null = null;
	export let kpis: DashboardOverview['kpis'] | null = null;

	const HEALTH_REFRESH_MS = 10_000;
	const SETTINGS_REFRESH_MS = 300_000;

	let healthStatus: 'ok' | 'degraded' | 'down' | 'unknown' = 'unknown';
	let issues: string[] = [];
	let lastGoodPollAt: number | null = null;
	let nowTick = Date.now();
	let mode = '';
	let exchange = '';
	let testnet: boolean | null = null;

	let healthTimer: ReturnType<typeof setInterval> | null = null;
	let settingsTimer: ReturnType<typeof setInterval> | null = null;
	let clockTimer: ReturnType<typeof setInterval> | null = null;

	async function pollHealth(): Promise<void> {
		try {
			const health = await getTaskHealth();
			healthStatus = health.status === 'ok' ? 'ok' : 'degraded';
			issues = health.issues;
			lastGoodPollAt = Date.now();
		} catch {
			healthStatus = 'down';
		}
	}

	async function pollSettings(): Promise<void> {
		try {
			const settings = await getSettings();
			mode = String(settings.trading_mode ?? '').trim();
			exchange = String(settings.exchange ?? '').trim();
			testnet = typeof settings.hyperliquid_testnet === 'boolean' ? settings.hyperliquid_testnet : null;
		} catch {
			// badge keeps last known values; health dot covers reachability
		}
	}

	$: pollAgeSeconds = lastGoodPollAt === null ? null : Math.max(0, (nowTick - lastGoodPollAt) / 1000);
	$: pollStale = pollAgeSeconds !== null && pollAgeSeconds > 45;
	$: statusLabel =
		healthStatus === 'ok' && !pollStale
			? 'ALL SYSTEMS GO'
			: healthStatus === 'down' || pollStale
				? 'BACKEND UNREACHABLE'
				: 'DEGRADED';
	$: statusClass =
		healthStatus === 'ok' && !pollStale
			? 'bg-emerald-500'
			: healthStatus === 'down' || pollStale
				? 'bg-red-500'
				: 'bg-amber-400';
	$: modeBadge = (() => {
		if (!mode) return '…';
		const venue = exchange ? exchange.toUpperCase() : '';
		const net = testnet === null ? '' : testnet ? 'TESTNET' : 'MAINNET';
		return [mode.toUpperCase(), venue, net].filter(Boolean).join(' · ');
	})();
	$: liveDanger = mode === 'live' && testnet === false;

	onMount(() => {
		void pollHealth();
		void pollSettings();
		healthTimer = setInterval(() => void pollHealth(), HEALTH_REFRESH_MS);
		settingsTimer = setInterval(() => void pollSettings(), SETTINGS_REFRESH_MS);
		clockTimer = setInterval(() => (nowTick = Date.now()), 1000);
	});
	onDestroy(() => {
		if (healthTimer) clearInterval(healthTimer);
		if (settingsTimer) clearInterval(settingsTimer);
		if (clockTimer) clearInterval(clockTimer);
	});
</script>

<div
	class="flex flex-wrap items-center gap-x-4 gap-y-1 border border-[#222] bg-[#050505] px-3 py-2"
	data-testid="ops-header-strip"
>
	<span class="flex items-center gap-2">
		<span class="relative flex h-3 w-3">
			{#if healthStatus === 'ok' && !pollStale}
				<span class="absolute inline-flex h-full w-full animate-ping rounded-full {statusClass} opacity-40"></span>
			{/if}
			<span class="relative inline-flex h-3 w-3 rounded-full {statusClass}"></span>
		</span>
		<span class="text-sm font-semibold tracking-wide {healthStatus === 'ok' && !pollStale ? 'text-emerald-300' : healthStatus === 'down' || pollStale ? 'text-red-300' : 'text-amber-300'}">
			{statusLabel}
		</span>
	</span>

	<span class="border px-2 py-0.5 font-mono text-[11px] {liveDanger ? 'border-red-600 bg-red-950/40 text-red-300' : 'border-[#333] bg-black text-gray-300'}" title="Execution mode · venue · network">
		{modeBadge}
	</span>

	{#if autopilot}
		<span class="flex items-center gap-1.5 text-[11px]">
			<span class="uppercase text-gray-500">Autopilot</span>
			<span class={autopilot.paused ? 'text-amber-400' : autopilot.running ? 'text-emerald-400' : 'text-red-400'}>
				{autopilot.paused ? 'paused' : autopilot.running ? 'running' : 'stopped'}
			</span>
			<span class="text-gray-600">·</span>
			<span class="text-gray-400">{autopilot.active_workers}w / {autopilot.queued_jobs}q</span>
			{#if autopilot.dead_letter_jobs > 0}
				<span class="text-red-400">· {autopilot.dead_letter_jobs} dead-letter</span>
			{/if}
		</span>
	{/if}

	{#if kpis}
		<span class="flex items-center gap-3 font-mono text-[11px]" data-testid="ops-header-kpis">
			<span title="Strategies in the active pipeline"><span class="text-gray-500">pipe</span> <span class="text-gray-200">{kpis.pipeline_count}</span></span>
			<span title="Total strategies backtested"><span class="text-gray-500">tested</span> <span class="text-gray-200">{kpis.total_tested}</span></span>
			<span title="Best Sharpe on the books"><span class="text-gray-500">best</span> <span class="text-gray-200">{kpis.best_sharpe.toFixed(2)}</span></span>
			<span title="Signals fired today"><span class="text-gray-500">sig</span> <span class="text-gray-200">{kpis.signals_today}</span></span>
		</span>
	{/if}

	{#if issues.length > 0}
		<span class="max-w-[36ch] truncate text-[11px] text-amber-300" title={issues.join('\n')}>⚠ {issues[0]}</span>
	{/if}

	<span class="ml-auto flex items-center gap-3 font-mono text-[11px] text-gray-500">
		<span title="Age of the last successful health check">
			checked {pollAgeSeconds === null ? '—' : `${Math.round(pollAgeSeconds)}s`} ago
		</span>
		<span class="text-gray-300">{new Date(nowTick).toLocaleTimeString()}</span>
	</span>
</div>
