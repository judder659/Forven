<script lang="ts">
	/**
	 * Scheduler watch: failing jobs first (the thing to act on), then what runs
	 * next with live countdowns — so the operator can see the autonomous loop's
	 * immediate future, not just its past.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getSchedulerJobs, type SchedulerJobSummary } from '$lib/api/dashboard';

	const REFRESH_MS = 30_000;

	let jobs: SchedulerJobSummary[] = [];
	let loaded = false;
	let error = '';
	let consecutiveFailures = 0;
	let nowTick = Date.now();
	let timer: ReturnType<typeof setInterval> | null = null;
	let clock: ReturnType<typeof setInterval> | null = null;

	async function load(): Promise<void> {
		try {
			jobs = await getSchedulerJobs();
			loaded = true;
			error = '';
			consecutiveFailures = 0;
		} catch (err) {
			consecutiveFailures += 1;
			if (consecutiveFailures >= 2) {
				error = err instanceof Error ? err.message : 'Scheduler unavailable.';
			}
		}
	}

	$: failing = jobs.filter(
		(job) => job.enabled && (job.lastError || (job.lastStatus && job.lastStatus !== 'ok')),
	);
	$: running = jobs.filter((job) => job.enabled && job.runningSince);
	$: upcoming = jobs
		.filter((job) => job.enabled && job.nextRunAt && !job.runningSince)
		.map((job) => ({ job, dueMs: Date.parse(job.nextRunAt as string) }))
		.filter((entry) => Number.isFinite(entry.dueMs))
		.sort((a, b) => a.dueMs - b.dueMs)
		.slice(0, 7);

	function countdown(dueMs: number): string {
		const delta = (dueMs - nowTick) / 1000;
		if (delta <= 0) return 'due';
		if (delta < 90) return `${Math.round(delta)}s`;
		if (delta < 5400) return `${Math.round(delta / 60)}m`;
		return `${(delta / 3600).toFixed(1)}h`;
	}

	function shortName(job: SchedulerJobSummary): string {
		return job.name || job.id.replace(/^forven-/, '');
	}

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), REFRESH_MS);
		clock = setInterval(() => (nowTick = Date.now()), 5_000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
		if (clock) clearInterval(clock);
	});
</script>

<div class="flex h-full min-h-0 flex-col border border-[#222] bg-[#050505]" data-testid="scheduler-watch-panel">
	<div class="flex items-center justify-between border-b border-[#1a1a1a] px-2.5 py-1.5">
		<h2 class="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Scheduler</h2>
		<span class="font-mono text-[10px] {failing.length > 0 ? 'text-red-400' : 'text-gray-500'}">
			{loaded ? (failing.length > 0 ? `${failing.length} failing` : `${jobs.filter((j) => j.enabled).length} jobs`) : '…'}
		</span>
	</div>
	<div class="min-h-0 flex-1 overflow-y-auto px-2.5 py-1.5 font-mono text-[11px]">
		{#if error && !loaded}
			<div class="text-red-300">{error}</div>
		{:else if !loaded}
			<div class="text-gray-500">Loading…</div>
		{:else}
			{#each failing.slice(0, 4) as job (job.id)}
				<div class="mb-1 flex items-start gap-2">
					<span class="mt-0.5 shrink-0 text-red-400">●</span>
					<span class="min-w-0 flex-1 truncate text-red-300" title={job.lastError ?? job.lastStatus}>
						{shortName(job)} — {job.lastError ?? job.lastStatus}
					</span>
				</div>
			{/each}
			{#each running as job (job.id)}
				<div class="flex items-center justify-between gap-2">
					<span class="min-w-0 truncate text-white" title={job.id}>{shortName(job)}</span>
					<span class="shrink-0 text-[#888]">running</span>
				</div>
			{/each}
			{#each upcoming as entry (entry.job.id)}
				<div class="flex items-center justify-between gap-2">
					<span class="min-w-0 truncate text-gray-400" title={entry.job.id}>{shortName(entry.job)}</span>
					<span class="shrink-0 {entry.dueMs - nowTick <= 0 ? 'text-amber-400' : 'text-gray-500'}">{countdown(entry.dueMs)}</span>
				</div>
			{:else}
				{#if failing.length === 0 && running.length === 0}
					<div class="text-gray-600">No scheduled jobs.</div>
				{/if}
			{/each}
		{/if}
	</div>
</div>
