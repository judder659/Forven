<script lang="ts">
	// Jobs: everything the data layer is doing or did. Running first (progress,
	// ETA, Cancel), then queued, failed (in words, with Retry) and finished.
	// Automatic collection ticks are hidden unless asked for; each is one row.
	import { onDestroy } from 'svelte';
	import { listJobs, type JobsQuery } from '$lib/api/dataManager';
	import type { DataJob, DataJobKind, DataJobStatus } from '$lib/api/dataManagerTypes';
	import JobRow from '$lib/components/data-manager/JobRow.svelte';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import { formatCount, JOB_KIND_LABEL } from '$lib/components/data-manager/format';
	import { DM } from '$lib/components/data-manager/links';
	import { createRequestGuard, jobsSummary, loading, settle, type Loadable } from '$lib/stores/dataManager';

	type StatusFilter = 'all' | 'active' | 'failed' | 'finished';
	const STATUS: Record<StatusFilter, { label: string; statuses: DataJobStatus[] | undefined }> = {
		all: { label: 'Everything', statuses: undefined },
		active: { label: 'Running & queued', statuses: ['running', 'queued'] },
		failed: { label: 'Failed', statuses: ['failed', 'interrupted'] },
		finished: { label: 'Finished', statuses: ['succeeded', 'cancelled'] },
	};
	const ORIGINS: Array<{ value: string; label: string }> = [
		{ value: '', label: 'Everyone' },
		{ value: 'user', label: 'You' },
		{ value: 'sla', label: 'Automatic' },
		{ value: 'strategy:', label: 'Strategy demand' },
		{ value: 'universe', label: 'Universe' },
		{ value: 'system', label: 'System' },
	];
	const GROUPS: Array<{ key: string; label: string; statuses: DataJobStatus[] }> = [
		{ key: 'running', label: 'Running', statuses: ['running'] },
		{ key: 'queued', label: 'Queued', statuses: ['queued'] },
		{ key: 'failed', label: 'Failed', statuses: ['failed', 'interrupted'] },
		{ key: 'finished', label: 'Finished', statuses: ['succeeded', 'cancelled'] },
	];
	const LIMIT = 50;

	let status: StatusFilter = 'all';
	let origin = '';
	let kind: DataJobKind | '' = '';
	let showRoutine = false;
	let limit = LIMIT;
	let jobs: Loadable<{ total: number; jobs: DataJob[] }> = loading();
	const guard = createRequestGuard();

	function query(): JobsQuery {
		return {
			status: STATUS[status].statuses,
			origin: origin || undefined,
			kind: kind ? [kind] : undefined,
			routine: showRoutine ? undefined : false,
			limit,
		};
	}

	async function load() {
		const { signal, current } = guard.next();
		const next = await settle(listJobs(query(), signal), jobs);
		if (current()) jobs = next;
	}

	// Filters changed: reload from the first page.
	$: filterKey = JSON.stringify([status, origin, kind, showRoutine]);
	let appliedKey = '';
	$: if (filterKey !== appliedKey) {
		appliedKey = filterKey;
		limit = LIMIT;
		void load();
	}
	// Follow the summary poll (fast while anything runs).
	let seenAt = -1;
	$: if ($jobsSummary.at !== seenAt) {
		seenAt = $jobsSummary.at;
		if (appliedKey) void load();
	}
	onDestroy(() => guard.cancel());

	$: list = jobs.data?.jobs ?? [];
	$: groups = GROUPS.map((g) => ({ ...g, jobs: list.filter((j) => g.statuses.includes(j.status)) })).filter((g) => g.jobs.length);
	const chip = (on: boolean) =>
		`border px-2.5 py-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] transition-colors ${on ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2 hover:text-sc-ink'}`;
</script>

<svelte:head><title>Data · Jobs | Forven</title></svelte:head>

<div class="space-y-3 p-4 pb-24">
	<div class="flex flex-wrap items-center gap-2">
		<div class="flex flex-wrap gap-1" role="group" aria-label="Status">
			{#each Object.entries(STATUS) as [key, s] (key)}
				<button type="button" class={chip(status === key)} aria-pressed={status === key} on:click={() => (status = key as StatusFilter)}>{s.label}</button>
			{/each}
		</div>
		<label class="flex items-center gap-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Started by
			<select bind:value={origin} class="rounded-md border border-sc-line2 bg-sc-bg px-1.5 py-1 text-[10px] normal-case text-sc-ink outline-none focus:border-sc-ink">
				{#each ORIGINS as o}<option value={o.value}>{o.label}</option>{/each}
			</select>
		</label>
		<label class="flex items-center gap-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Kind
			<select bind:value={kind} class="rounded-md border border-sc-line2 bg-sc-bg px-1.5 py-1 text-[10px] normal-case text-sc-ink outline-none focus:border-sc-ink">
				<option value="">All kinds</option>
				{#each Object.entries(JOB_KIND_LABEL) as [value, label]}<option {value}>{label}</option>{/each}
			</select>
		</label>
		<label class="flex items-center gap-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3" title="Automatic collection runs every minute; each run is one row">
			<input type="checkbox" bind:checked={showRoutine} class="accent-white" /> Show collection ticks
		</label>
		{#if jobs.data}<span class="ml-auto text-[10px] text-sc-ink3"><span class="font-mono text-sc-ink2">{formatCount(jobs.data.total)}</span> jobs</span>{/if}
	</div>

	<section class="rounded-md border border-sc-line bg-sc-panel" aria-label="Jobs">
		<SectionState state={jobs} what="The job list" endpoint="GET /api/data/jobs" rows={6} on:retry={load}>
			{#if !groups.length}
				<div class="px-4 py-8 text-center">
					<p class="text-[13px] text-sc-ink">No jobs match.</p>
					<p class="mt-1 text-[11px] text-sc-ink3">
						Downloads, history extensions, imports and repairs you start appear here, and so does automatic collection work.
						<a href="{DM}/get" class="text-sc-ink underline">Get data</a> to start one.
					</p>
				</div>
			{:else}
				{#each groups as group (group.key)}
					<h2 class="text-[14px] font-semibold border-b border-sc-line bg-sc-panel px-3 py-1.5 text-sc-ink3">{group.label} <span class="font-normal text-sc-ink3">· {group.jobs.length}</span></h2>
					{#each group.jobs as job (job.id)}<JobRow {job} on:changed={load} />{/each}
				{/each}
				{#if jobs.data && jobs.data.total > list.length}
					<button type="button" on:click={() => { limit += LIMIT; void load(); }}
						class="w-full border-t border-sc-line px-3 py-2 text-left text-[12px] text-sc-ink2 hover:text-sc-ink">
						Show more ({formatCount(jobs.data.total - list.length)} older)</button>
				{/if}
			{/if}
		</SectionState>
	</section>
</div>
