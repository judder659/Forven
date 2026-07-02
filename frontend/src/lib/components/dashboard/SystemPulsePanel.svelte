<script lang="ts">
	/**
	 * Always-on system pulse: scheduler heartbeat age, worker loop freshness,
	 * task queues, and overdue/long-running scheduler jobs — the signals that
	 * tell the operator the autonomous loop is actually alive, not just that
	 * the process exists. Polls with backoff and survives backend restarts.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getSystemPulse, type SystemPulse } from '$lib/api/dashboard';

	const REFRESH_MS = 15_000;

	let pulse: SystemPulse | null = null;
	let error = '';
	let consecutiveFailures = 0;
	let timer: ReturnType<typeof setInterval> | null = null;

	async function load(): Promise<void> {
		try {
			pulse = await getSystemPulse();
			error = '';
			consecutiveFailures = 0;
		} catch (err) {
			consecutiveFailures += 1;
			// One missed poll is usually a restart blip; only surface after two.
			if (consecutiveFailures >= 2) {
				error = err instanceof Error ? err.message : 'Health endpoint unreachable.';
			}
		}
	}

	function schedulerTone(age: number | null): string {
		if (age === null) return 'text-gray-500';
		if (age <= 60) return 'text-emerald-400';
		if (age <= 300) return 'text-amber-400';
		return 'text-red-400';
	}

	function formatAge(age: number | null): string {
		if (age === null) return '—';
		if (age < 90) return `${Math.round(age)}s`;
		if (age < 5400) return `${Math.round(age / 60)}m`;
		return `${(age / 3600).toFixed(1)}h`;
	}

	$: staleQueueCount = pulse
		? pulse.queues.agent_stale_pending +
			pulse.queues.agent_stale_running +
			pulse.queues.brain_stale_pending +
			pulse.queues.brain_stale_running
		: 0;

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), REFRESH_MS);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<div class="flex h-full flex-col border border-[#222] bg-[#050505] p-2.5" data-testid="system-pulse-panel">
	<div class="mb-1.5 flex items-center justify-between">
		<h2 class="text-[10px] font-semibold uppercase tracking-wider text-gray-500">System pulse</h2>
		{#if pulse}
			<span class={pulse.status === 'ok' ? 'text-[10px] uppercase text-emerald-400' : 'text-[10px] uppercase text-red-400'}>
				{pulse.status === 'ok' ? '● healthy' : `● ${pulse.status}`}
			</span>
		{/if}
	</div>

	{#if error && !pulse}
		<div class="text-xs text-red-300">{error}</div>
	{:else if !pulse}
		<div class="text-xs text-gray-500">Checking…</div>
	{:else}
		<div class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 font-mono text-xs">
			<div class="text-gray-500">Scheduler beat</div>
			<div class={schedulerTone(pulse.schedulerAgeSeconds)}>{formatAge(pulse.schedulerAgeSeconds)} ago</div>

			{#each pulse.workerLoops as loop}
				<div class="text-gray-500">{loop.name} loop</div>
				<div class={loop.fresh ? 'text-emerald-400' : 'text-red-400'}>
					{loop.fresh ? 'fresh' : 'STALE'} · {formatAge(loop.ageSeconds)}
				</div>
			{/each}

			<div class="text-gray-500">Queue agent</div>
			<div class="text-gray-300">{pulse.queues.agent_pending} pending · {pulse.queues.agent_running} running</div>

			<div class="text-gray-500">Queue brain</div>
			<div class="text-gray-300">{pulse.queues.brain_pending} pending · {pulse.queues.brain_running} running</div>

			<div class="text-gray-500">Stale tasks</div>
			<div class={staleQueueCount > 0 ? 'text-amber-400' : 'text-gray-300'}>{staleQueueCount}</div>

			<div class="text-gray-500">Overdue jobs</div>
			<div class={pulse.overdueJobs > 0 ? 'text-red-400' : 'text-gray-300'}>{pulse.overdueJobs}</div>

			<div class="text-gray-500">Long-running</div>
			<div class={pulse.longRunningJobs > 0 ? 'text-amber-400' : 'text-gray-300'}>{pulse.longRunningJobs}</div>
		</div>

		{#if pulse.overdueJobIds.length > 0}
			<div class="mt-2 truncate text-[11px] text-red-300" title={pulse.overdueJobIds.join(', ')}>
				overdue: {pulse.overdueJobIds.slice(0, 3).join(', ')}{pulse.overdueJobIds.length > 3 ? '…' : ''}
			</div>
		{/if}
		{#if pulse.issues.length > 0}
			<div class="mt-2 space-y-0.5">
				{#each pulse.issues.slice(0, 4) as issue}
					<div class="truncate text-[11px] text-amber-300" title={issue}>⚠ {issue}</div>
				{/each}
			</div>
		{/if}
		{#if error}
			<div class="mt-2 text-[11px] text-red-300">{error} (showing last good data)</div>
		{/if}
	{/if}
</div>
