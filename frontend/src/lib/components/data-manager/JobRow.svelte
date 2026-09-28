<script lang="ts">
	// One data job: status in words, what it works on, progress with an ETA,
	// the error in plain language, and Cancel / Retry.
	import { createEventDispatcher } from 'svelte';
	import { cancelJob, retryJob } from '$lib/api/dataManager';
	import type { DataJob } from '$lib/api/dataManagerTypes';
	import { clock } from '$lib/stores/dataManager';
	import { runAction } from './actions';
	import {
		errorText,
		formatCount,
		formatDuration,
		formatRelative,
		formatUtc,
		JOB_STATUS_LABEL,
		jobEtaSeconds,
		jobKindLabel,
		jobSeriesText,
		jobStatusClass,
		originLabel,
		progressFraction,
		progressText,
	} from './format';

	export let job: DataJob;

	const dispatch = createEventDispatcher<{ changed: DataJob }>();
	let busy = false;

	$: running = job.status === 'running';
	$: active = running || job.status === 'queued';
	$: fraction = progressFraction(job.progress);
	$: eta = jobEtaSeconds(job, $clock);
	$: when = running ? job.started_at : job.status === 'queued' ? job.created_at : job.finished_at ?? job.updated_at;
	$: whenWord = running ? 'started' : job.status === 'queued' ? 'queued' : 'finished';
	$: series = jobSeriesText(job);
	$: tick = job.routine && job.result && typeof job.result === 'object' ? (job.result as Record<string, number>) : null;

	async function cancel() {
		busy = true;
		const next = await runAction('Cancelling the job', () => cancelJob(job.id), {
			success: (j) => (j.status === 'cancelled' ? `Cancelled “${j.title}”` : `Asked “${j.title}” to stop at its next checkpoint`),
		});
		busy = false;
		if (next) dispatch('changed', next);
	}

	async function retry() {
		busy = true;
		const next = await runAction('Retrying the job', () => retryJob(job.id), { success: (j) => `Queued “${j.title}” again` });
		busy = false;
		if (next) dispatch('changed', next);
	}
</script>

<div class="border-b border-[#141414] px-3 py-2 last:border-b-0" data-testid="job-row">
	<div class="flex items-start gap-2">
		<div class="min-w-0 flex-1">
			<div class="flex min-w-0 items-center gap-2">
				<span class="shrink-0 border px-1 py-px text-[9px] font-bold uppercase tracking-wider {jobStatusClass(job.status)}">{JOB_STATUS_LABEL[job.status] ?? job.status}</span>
				<span class="truncate text-[12px] text-white" title={job.title}>{job.title}</span>
			</div>
			<div class="mt-0.5 flex flex-wrap gap-x-1.5 text-[10px] text-[#666]">
				<span>{jobKindLabel(job.kind)}</span>
				<span aria-hidden="true">·</span>
				<span>{originLabel(job.origin)}</span>
				{#if series && !job.title.includes(series.split(' ')[0])}<span aria-hidden="true">·</span><span class="font-mono">{series}</span>{/if}
				{#if when}<span aria-hidden="true">·</span><span title={formatUtc(when, { seconds: true })}>{whenWord} {formatRelative(when, $clock)}</span>{/if}
				{#if job.attempts > 1}<span aria-hidden="true">·</span><span>attempt {job.attempts}</span>{/if}
			</div>
		</div>
		<div class="flex shrink-0 gap-1">
			{#if active}
				<button type="button" on:click={cancel} disabled={busy || job.cancel_requested}
					class="border border-[#2a2a2a] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[#aaa] hover:border-red-500 hover:text-red-400 disabled:opacity-40"
					aria-label={`Cancel ${job.title}`}>{job.cancel_requested ? 'Stopping…' : 'Cancel'}</button>
			{:else if job.retryable}
				<button type="button" on:click={retry} disabled={busy}
					class="border border-[#2a2a2a] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[#aaa] hover:border-white hover:text-white disabled:opacity-40"
					aria-label={`Retry ${job.title}`}>Retry</button>
			{/if}
		</div>
	</div>

	{#if running}
		<div class="mt-1.5">
			<div class="h-1 w-full bg-[#141414]" role="progressbar" aria-label={`${job.title} progress`}
				aria-valuemin="0" aria-valuemax="100" aria-valuenow={fraction == null ? undefined : Math.round(fraction * 100)}>
				{#if fraction == null}
					<div class="h-full w-1/3 animate-pulse bg-sky-500/60"></div>
				{:else}
					<div class="h-full bg-sky-400 transition-[width] duration-500" style="width: {Math.max(2, fraction * 100)}%"></div>
				{/if}
			</div>
			<div class="mt-1 flex flex-wrap justify-between gap-x-2 text-[10px] text-[#888]">
				<span class="font-mono tabular-nums">{progressText(job.progress) || 'Working…'}</span>
				{#if eta != null}<span>about {formatDuration(eta)} left</span>{/if}
			</div>
		</div>
	{/if}
	{#if job.message && active}<div class="mt-1 truncate text-[10px] text-[#777]" title={job.message}>{job.message}</div>{/if}
	{#if tick}
		<div class="mt-1 font-mono text-[10px] tabular-nums text-[#888]">
			refreshed {formatCount(tick.refreshed)} · +{formatCount(tick.bars_added)} bars{#if tick.failed} · <span class="text-amber-400">{tick.failed} failed</span>{/if}{#if tick.deferred} · {formatCount(tick.deferred)} deferred{/if}
		</div>
	{/if}
	{#if job.error}
		<div class="mt-1 text-[11px] {job.status === 'interrupted' ? 'text-amber-400' : 'text-red-400'}" title={job.error.message}>
			{errorText(job.error.code)}{#if job.error.message && job.error.code !== 'cancelled'}<span class="text-[#777]"> · {job.error.message}</span>{/if}
		</div>
	{/if}
</div>
