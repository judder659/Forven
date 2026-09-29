<script lang="ts">
	// Inline review before deleting series: who reads each one, whether the
	// collector would download it again, a typed confirmation, and the trash
	// retention. Blocking consumers (live, paper, pipeline) need an explicit override.
	import { createEventDispatcher, onMount } from 'svelte';
	import { isRouteMissingError } from '$lib/api/core';
	import { checkDelete, deleteSeries, getTrash } from '$lib/api/dataManager';
	import type { DataStream, DeleteCheck, DeleteResult } from '$lib/api/dataManagerTypes';
	import { errorMessage } from '$lib/stores/dataManager';
	import TypedConfirm from './TypedConfirm.svelte';
	import { keyOf, runAction } from './actions';
	import { formatBytes, formatCount, plural, streamLabel } from './format';

	export let series: Array<{ symbol: string; display_symbol: string; timeframe: string; stream: DataStream; venue: string }>;

	const dispatch = createEventDispatcher<{ done: DeleteResult; cancel: void }>();
	let checks: Array<{ name: string; check: DeleteCheck | null; error: string }> = [];
	let status: 'loading' | 'ready' | 'unavailable' | 'error' = 'loading';
	let loadError = '';
	let retention: number | null = null;
	let override = false;
	let busy = false;

	onMount(async () => {
		getTrash().then((t) => (retention = t.retention_days)).catch(() => undefined);
		const out: typeof checks = new Array(series.length);
		let next = 0;
		let missing = false;
		const worker = async () => {
			while (next < series.length) {
				const i = next++;
				const s = series[i];
				const name = `${s.display_symbol} ${s.timeframe}${s.stream === 'ohlcv' ? '' : ` ${streamLabel(s.stream).toLowerCase()}`}${s.venue === 'canonical' ? '' : ` (${s.venue})`}`;
				try {
					out[i] = { name, check: await checkDelete(keyOf(s)), error: '' };
				} catch (error) {
					if (isRouteMissingError(error)) missing = true;
					out[i] = { name, check: null, error: errorMessage(error) };
				}
			}
		};
		await Promise.all(Array.from({ length: Math.min(6, series.length) }, worker));
		checks = out;
		if (missing) status = 'unavailable';
		else if (out.every((c) => !c.check)) {
			status = 'error';
			loadError = out[0]?.error ?? 'The delete check failed.';
		} else status = 'ready';
	});

	$: ready = checks.filter((c) => c.check);
	$: blocking = ready.filter((c) => c.check?.blocking);
	$: comingBack = ready.filter((c) => c.check?.will_rebootstrap);
	$: bytes = ready.reduce((sum, c) => sum + (c.check?.bytes ?? 0), 0);
	$: rows = ready.reduce((sum, c) => sum + (c.check?.rows ?? 0), 0);
	$: phrase = series.length === 1 ? ready[0]?.check?.confirm_phrase ?? `delete ${series[0]?.symbol} ${series[0]?.timeframe}` : `delete ${series.length} series`;
	$: failedChecks = checks.filter((c) => !c.check);

	async function confirm() {
		busy = true;
		const result = await runAction(
			'Deleting',
			() => deleteSeries({ series: series.map(keyOf), confirm: phrase, override_consumers: override || undefined }),
			{
				success: (r) =>
					`Moved ${plural(r.trashed.length, 'series', 'series')} to the trash${retention ? ` for ${retention} days` : ''}${r.skipped.length ? `; skipped ${r.skipped.length}` : ''}.`,
				poke: false,
			},
		);
		busy = false;
		if (result) dispatch('done', result);
	}
</script>

<section class="border border-red-900/70 bg-[#070303]" aria-labelledby="dm-delete-title" data-testid="delete-review">
	<header class="flex items-center gap-2 border-b border-red-950 px-3 py-1.5">
		<h3 id="dm-delete-title" class="text-[13px] font-semibold text-red-300">Delete {plural(series.length, 'series', 'series')}</h3>
		<span class="text-[10px] text-sc-ink2">
			moves {series.length === 1 ? 'it' : 'them'} to the trash{retention ? ` for ${retention} days` : ''}; restore from Storage until then
		</span>
		<button type="button" on:click={() => dispatch('cancel')} aria-label="Close delete review" class="ml-auto px-1 text-sc-ink3 hover:text-sc-ink">✕</button>
	</header>
	<div class="space-y-2 p-3">
		{#if status === 'loading'}
			<p class="text-[11px] text-sc-ink2" aria-busy="true">Checking what reads {series.length === 1 ? 'this series' : 'these series'}…</p>
		{:else if status === 'unavailable'}
			<p class="text-[12px] text-sc-ink2" role="status">
				Safe delete is not available on this backend yet (<span class="font-mono text-[11px]">GET /api/data/delete/check</span>). Nothing was deleted.
			</p>
		{:else if status === 'error'}
			<p class="text-[12px] text-red-400" role="alert">Could not check the series: {loadError}</p>
		{:else}
			<div class="max-h-56 space-y-1 overflow-y-auto pr-1">
				{#each checks as item (item.name)}
					<div class="border-l-2 py-0.5 pl-2 text-[11px] {item.check?.blocking ? 'border-red-500' : 'border-sc-line2'}">
						<div class="flex flex-wrap items-baseline gap-x-2">
							<span class="font-bold text-sc-ink">{item.name}</span>
							{#if item.check}
								<span class="font-mono text-[10px] text-sc-ink2">{formatCount(item.check.rows)} rows · {formatBytes(item.check.bytes)}</span>
								{#if item.check.blocking}<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-red-400">in use</span>{/if}
								{#if !item.check.exists}<span class="text-[10px] text-sc-ink2">nothing stored</span>{/if}
							{:else}
								<span class="text-[10px] text-red-400">check failed: {item.error}</span>
							{/if}
						</div>
						{#if item.check?.consumers.length}
							<div class="text-[10px] text-sc-ink2">Read by {item.check.consumers.map((c) => `${c.id} ${c.name}${c.stage ? ` (${c.stage})` : c.status ? ` (${c.status})` : ''}`).join(', ')}</div>
						{/if}
						{#each item.check?.warnings ?? [] as warning}<div class="text-[10px] text-amber-400">{warning}</div>{/each}
					</div>
				{/each}
			</div>
			<p class="text-[11px] text-sc-ink2">
				Total: {formatCount(rows)} rows, {formatBytes(bytes)}.
				{#if comingBack.length && !ready.some((c) => c.check?.warnings.length)}<span class="text-amber-400">{comingBack.length === series.length ? (series.length === 1 ? 'It' : 'All of them') : plural(comingBack.length, 'series', 'series')} will be downloaded again by the collector, because a strategy or the research universe needs {comingBack.length === 1 ? 'it' : 'them'}.</span>{/if}
			</p>
			{#if failedChecks.length}
				<p class="text-[11px] text-red-400">{plural(failedChecks.length, 'series', 'series')} could not be checked and will be skipped by the server if unsafe.</p>
			{/if}
			{#if blocking.length}
				<label class="flex items-start gap-2 border border-red-900 bg-red-500/5 px-2.5 py-2 text-[11px] text-red-300">
					<input type="checkbox" bind:checked={override} class="mt-0.5 accent-red-500" />
					<span>Delete anyway. Live, paper or pipeline strategies read {blocking.length === 1 ? 'one of these series' : `${blocking.length} of these series`}; they fail their data checks until it is downloaded again.</span>
				</label>
			{/if}
			{#if !blocking.length || override}
				<TypedConfirm {phrase} action="Move to trash" {busy} on:confirm={confirm} on:cancel={() => dispatch('cancel')} />
			{/if}
		{/if}
		{#if status !== 'ready' || (blocking.length && !override)}
			<div class="flex justify-end">
				<button type="button" class="terminal-button text-[12px]" on:click={() => dispatch('cancel')}>Cancel</button>
			</div>
		{/if}
	</div>
</section>
