<script lang="ts">
	// The Jobs drawer: what is running now, what just finished, and what failed.
	// Non-modal (no backdrop): the page stays usable while it is open. Focus moves
	// in on open, Escape closes, and the layout returns focus to the indicator.
	import { createEventDispatcher, onDestroy, onMount, tick } from 'svelte';
	import { fly } from 'svelte/transition';
	import { listJobs } from '$lib/api/dataManager';
	import type { DataJob } from '$lib/api/dataManagerTypes';
	import { clock, createRequestGuard, jobsSummary, loading, settle, type Loadable } from '$lib/stores/dataManager';
	import JobRow from './JobRow.svelte';
	import SectionState from './SectionState.svelte';
	import { formatCount, formatRelative, formatUtc } from './format';
	import { DM } from './links';

	const dispatch = createEventDispatcher<{ close: void }>();
	const guard = createRequestGuard();
	let active: Loadable<DataJob[]> = loading();
	let recent: Loadable<DataJob[]> = loading();
	let closeButton: HTMLButtonElement | undefined;

	async function load() {
		const { signal, current } = guard.next();
		const [nextActive, nextRecent] = await Promise.all([
			settle(listJobs({ status: ['running', 'queued'], limit: 50 }, signal).then((r) => r.jobs), active),
			settle(listJobs({ routine: false, status: ['succeeded', 'failed', 'cancelled', 'interrupted'], limit: 12 }, signal).then((r) => r.jobs), recent),
		]);
		if (!current()) return;
		active = nextActive;
		recent = nextRecent;
	}

	// Reload with every summary poll: fast while jobs run, slow otherwise.
	let seenAt = -1;
	$: if ($jobsSummary.at !== seenAt) {
		seenAt = $jobsSummary.at;
		void load();
	}

	$: summary = $jobsSummary.data;
	$: routine = summary?.last_routine ?? null;
	$: routineResult = routine?.result && typeof routine.result === 'object' ? (routine.result as Record<string, number>) : null;

	onMount(async () => {
		await tick();
		closeButton?.focus();
	});
	onDestroy(() => guard.cancel());

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.stopPropagation();
			dispatch('close');
		}
	}
</script>

<div in:fly={{ x: 420, duration: 180 }} role="dialog" tabindex="-1" aria-modal="false" aria-labelledby="dm-jobs-title" on:keydown={onKey}
	class="fixed right-0 top-0 z-[60] flex h-full w-[420px] max-w-[92vw] flex-col border-l border-sc-line2 bg-sc-panel font-mono text-sc-ink shadow-[-16px_0_40px_rgba(0,0,0,0.7)]">
	<header class="flex items-start gap-3 border-b border-sc-line px-4 py-3">
		<div class="min-w-0 flex-1">
			<h2 id="dm-jobs-title" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Jobs</h2>
			<p class="mt-1 text-[11px] text-sc-ink2">
				{#if summary}
					{formatCount(summary.running)} running · {formatCount(summary.queued)} queued{#if summary.failed_24h} · <span class="text-red-400">{formatCount(summary.failed_24h)} failed in 24 h</span>{/if}
				{:else if $jobsSummary.status === 'unavailable'}
					Jobs are not available on this backend yet.
				{:else}
					Checking…
				{/if}
			</p>
		</div>
		<button bind:this={closeButton} type="button" on:click={() => dispatch('close')} aria-label="Close jobs"
			class="px-1.5 text-[14px] leading-none text-sc-ink3 hover:text-sc-ink">✕</button>
	</header>

	<div class="min-h-0 flex-1 overflow-y-auto">
		<section aria-labelledby="dm-jobs-active">
			<h3 id="dm-jobs-active" class="border-b border-sc-line bg-sc-panel px-4 py-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Running and queued</h3>
			<SectionState state={active} what="The job list" endpoint="GET /api/data/jobs" rows={3} on:retry={load}>
				{#if active.data?.length}
					{#each active.data as job (job.id)}<JobRow {job} on:changed={load} />{/each}
				{:else}
					<p class="px-4 py-4 text-[12px] leading-relaxed text-sc-ink3">
						Nothing is running. Downloads you start show their progress here, and you can leave the page while they run.
					</p>
				{/if}
			</SectionState>
		</section>
		<section aria-labelledby="dm-jobs-recent">
			<h3 id="dm-jobs-recent" class="border-y border-sc-line bg-sc-panel px-4 py-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Recently finished</h3>
			<SectionState state={recent} what="The job list" endpoint="GET /api/data/jobs" rows={3} on:retry={load}>
				{#if recent.data?.length}
					{#each recent.data as job (job.id)}<JobRow {job} on:changed={load} />{/each}
				{:else}
					<p class="px-4 py-4 text-[12px] text-sc-ink3">No finished jobs yet.</p>
				{/if}
			</SectionState>
		</section>
	</div>

	<footer class="border-t border-sc-line px-4 py-2.5 text-[10px] text-sc-ink3">
		{#if routine}
			<div title={formatUtc(routine.finished_at ?? routine.created_at, { seconds: true })}>
				Automatic collection ran {formatRelative(routine.finished_at ?? routine.created_at, $clock)}{#if routineResult} · refreshed {formatCount(routineResult.refreshed)} series{/if}
			</div>
		{/if}
		<a href="{DM}/jobs" on:click={() => dispatch('close')} class="mt-1 inline-block text-sc-ink2 underline-offset-2 hover:text-sc-ink hover:underline">All jobs and filters →</a>
	</footer>
</div>
