<script lang="ts">
	/**
	 * Data integrity at a glance: dataset inventory health, last ingestion,
	 * and — most importantly — metric quarantines (data_quality alerts) in the
	 * last 24h. A non-zero quarantine count after data convergence means an
	 * engine/data bug is being caught live and needs investigation.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getDataHealth, type DataHealth } from '$lib/api/data';
	import { getRecentSystemAlerts, type SystemAlertEntry } from '$lib/api/dashboard';

	const REFRESH_MS = 60_000;

	let health: DataHealth | null = null;
	let quarantines: SystemAlertEntry[] = [];
	let error = '';
	let consecutiveFailures = 0;
	let timer: ReturnType<typeof setInterval> | null = null;

	async function load(): Promise<void> {
		try {
			const [healthResult, alertsResult] = await Promise.allSettled([
				getDataHealth(),
				getRecentSystemAlerts(200),
			]);
			if (healthResult.status === 'fulfilled') health = healthResult.value;
			if (alertsResult.status === 'fulfilled') {
				const dayAgo = Date.now() - 24 * 3600 * 1000;
				quarantines = alertsResult.value.filter((alert) => {
					if (alert.source !== 'data_quality') return false;
					const ts = Date.parse(alert.createdAt.includes('T') ? alert.createdAt : `${alert.createdAt.replace(' ', 'T')}Z`);
					return Number.isNaN(ts) ? true : ts >= dayAgo;
				});
			}
			if (healthResult.status === 'fulfilled' || alertsResult.status === 'fulfilled') {
				error = '';
				consecutiveFailures = 0;
			} else {
				throw (healthResult as PromiseRejectedResult).reason;
			}
		} catch (err) {
			consecutiveFailures += 1;
			if (consecutiveFailures >= 2) {
				error = err instanceof Error ? err.message : 'Data health unavailable.';
			}
		}
	}

	function formatIngestion(ts: string | null): string {
		if (!ts) return '—';
		const parsed = Date.parse(ts);
		if (Number.isNaN(parsed)) return ts;
		const mins = Math.max(0, (Date.now() - parsed) / 60_000);
		if (mins < 90) return `${Math.round(mins)}m ago`;
		if (mins < 48 * 60) return `${(mins / 60).toFixed(1)}h ago`;
		return `${(mins / 1440).toFixed(1)}d ago`;
	}

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), REFRESH_MS);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<div class="flex h-full flex-col border border-[#222] bg-[#050505] p-2.5" data-testid="data-integrity-panel">
	<div class="mb-1.5 flex items-center justify-between">
		<h2 class="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Data integrity</h2>
		<a href="/data" class="text-[10px] uppercase text-[#555] hover:text-white">data manager →</a>
	</div>

	{#if error && !health}
		<div class="text-xs text-red-300">{error}</div>
	{:else if !health}
		<div class="text-xs text-gray-500">Checking…</div>
	{:else}
		<div class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 font-mono text-xs">
			<div class="text-gray-500">Quarantines 24h</div>
			<div class={quarantines.length > 0 ? 'text-red-400' : 'text-emerald-400'}>{quarantines.length}</div>

			<div class="text-gray-500">Datasets</div>
			<div class="text-gray-300">{health.dataset_count}</div>

			<div class="text-gray-500">Last ingestion</div>
			<div class="text-gray-300">{formatIngestion(health.last_ingestion_at)}</div>

			<div class="text-gray-500">Quality avg</div>
			<div class={health.quality_avg_score === null ? 'text-gray-500' : health.quality_avg_score >= 90 ? 'text-emerald-400' : health.quality_avg_score >= 70 ? 'text-amber-400' : 'text-red-400'}>
				{health.quality_avg_score === null ? '—' : Math.round(health.quality_avg_score)}
			</div>

			<div class="text-gray-500">Orphans</div>
			<div class={health.orphan_count > 0 ? 'text-amber-400' : 'text-gray-300'}>{health.orphan_count}</div>
		</div>

		{#if quarantines.length > 0}
			<div class="mt-2 space-y-0.5 overflow-hidden">
				{#each quarantines.slice(0, 3) as q}
					<div class="truncate text-[11px] text-red-300" title={q.message}>⛔ {q.message}</div>
				{/each}
				{#if quarantines.length > 3}
					<div class="text-[11px] text-gray-500">… {quarantines.length - 3} more in the alerts feed</div>
				{/if}
			</div>
		{:else}
			<div class="mt-2 text-[11px] text-gray-600">No metric quarantines — engine and data agree.</div>
		{/if}
		{#if error}
			<div class="mt-2 text-[11px] text-red-300">{error} (showing last good data)</div>
		{/if}
	{/if}
</div>
