<script lang="ts">
	/** Identity, the go-live ceiling (live), decision parameters and the execution profile. */
	import { createEventDispatcher } from 'svelte';
	import type { DeskMode } from '$lib/api/desk';
	import { tradeModeLabel } from '$lib/utils/tradingDesk/describe';
	import { fmtDateTime, fmtUsd, num } from '$lib/utils/tradingDesk/format';
	import type { DeskRow } from '$lib/utils/tradingDesk/rows';

	export let mode: DeskMode;
	export let row: DeskRow;
	export let sliceUsd: number | null = null;
	export let busy = false;

	const dispatch = createEventDispatcher<{ saveCeiling: number }>();

	let ceilingInput: string | number | null = '';
	let seededFor = '';
	$: if (row.sid !== seededFor) {
		seededFor = row.sid;
		const current = num(row.session.live_notional_ceiling_usd);
		ceilingInput = current !== null ? current : '';
	}
	$: ceilingValue = num(ceilingInput);
	$: current = num(row.session.live_notional_ceiling_usd);
	$: params = Object.entries(row.session.decision_params ?? row.session.params ?? {}).filter(([key]) => !key.startsWith('_') && key !== 'execution_profile');
	$: profileRaw = (row.session.decision_params ?? row.session.params ?? {}).execution_profile;
	$: profile = profileRaw && typeof profileRaw === 'object' ? Object.entries(profileRaw as Record<string, unknown>).filter(([, value]) => value !== null && value !== undefined) : [];

	function show(value: unknown): string {
		if (value === null || value === undefined) return '—';
		if (typeof value === 'object') {
			try {
				return JSON.stringify(value);
			} catch {
				return String(value);
			}
		}
		return String(value);
	}
</script>

<div class="grid gap-3.5" data-testid="desk-details">
	<div class="grid gap-2">
		<h3 class="text-[13px] font-semibold text-sc-ink">{row.sid} · {row.name}</h3>
		<dl class="grid grid-cols-2 gap-x-3.5 gap-y-1.5 text-[12.5px]">
			<div><dt class="text-[11.5px] text-sc-ink3">Market</dt><dd class="font-plex-mono text-sc-ink">{row.session.symbol} · {row.timeframe}</dd></div>
			<div><dt class="text-[11.5px] text-sc-ink3">Direction</dt><dd class="text-sc-ink">{tradeModeLabel(row.session.trade_mode)}</dd></div>
			<div><dt class="text-[11.5px] text-sc-ink3">Leverage</dt><dd class="font-plex-mono text-sc-ink">{row.session.leverage ?? 1}×</dd></div>
			<div><dt class="text-[11.5px] text-sc-ink3">Position model</dt><dd class="text-sc-ink">{row.session.position_model ?? '—'}</dd></div>
			<div><dt class="text-[11.5px] text-sc-ink3">{mode === 'live' ? 'Live since' : 'In paper since'}</dt><dd class="font-plex-mono text-sc-ink">{fmtDateTime(row.fleet?.live_since ?? row.session.started_at)}</dd></div>
			<div><dt class="text-[11.5px] text-sc-ink3">{mode === 'live' ? 'Capital slice' : 'Book'}</dt><dd class="font-plex-mono text-sc-ink">{mode === 'live' ? fmtUsd(sliceUsd) : fmtUsd(row.session.capital)}</dd></div>
		</dl>
	</div>
	{#if mode === 'live'}
		<div class="grid gap-1.5">
			<h3 class="text-[13px] font-semibold text-sc-ink">Go-live ceiling</h3>
			<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-1.5">
				<input class="w-full min-w-0 rounded border border-sc-line2 bg-sc-bg px-2 py-1 font-plex-mono text-[12.5px] text-sc-ink focus:border-sc-ink4 focus:outline-none" type="number" min="1" step="50" placeholder="Not set" aria-label="Go-live ceiling in USD" bind:value={ceilingInput} disabled={busy} />
				<button type="button" class="whitespace-nowrap rounded-md border border-sc-line2 bg-sc-panel2 px-2.5 py-1 text-[12px] font-medium text-sc-ink hover:border-sc-ink4 disabled:cursor-not-allowed disabled:opacity-45" disabled={busy || ceilingValue === null || ceilingValue <= 0 || ceilingValue === current} on:click={() => ceilingValue !== null && dispatch('saveCeiling', ceilingValue)}>Save ceiling</button>
			</div>
			<p class="text-[12px] text-sc-ink3">The largest order value this strategy may open live. Opens above it are refused, not downsized.{sliceUsd ? ` Its slice today is ${fmtUsd(sliceUsd)}.` : ''}</p>
		</div>
	{/if}
	<div class="grid gap-1.5">
		<h3 class="text-[13px] font-semibold text-sc-ink">Parameters</h3>
		{#if params.length}
			<div class="grid grid-cols-[minmax(0,1fr)_auto] text-[12px]">
				{#each params as [key, value]}
					<span class="border-b border-sc-line py-1 font-plex-mono text-sc-ink2">{key}</span>
					<span class="border-b border-sc-line py-1 text-right font-plex-mono text-sc-ink">{show(value)}</span>
				{/each}
			</div>
		{:else}
			<p class="text-[12px] text-sc-ink3">No parameters recorded.</p>
		{/if}
	</div>
	{#if profile.length}
		<div class="grid gap-1.5">
			<h3 class="text-[13px] font-semibold text-sc-ink">Execution profile</h3>
			<div class="grid grid-cols-[minmax(0,1fr)_auto] text-[12px]">
				{#each profile as [key, value]}
					<span class="border-b border-sc-line py-1 font-plex-mono text-sc-ink2">{key}</span>
					<span class="border-b border-sc-line py-1 text-right font-plex-mono text-sc-ink">{show(value)}</span>
				{/each}
			</div>
		</div>
	{/if}
	<a class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-center text-[12.5px] font-medium text-sc-ink no-underline hover:border-sc-ink4" href={`/lab/strategy/${encodeURIComponent(row.sid)}`}>Open {row.sid} in the Forge</a>
</div>
