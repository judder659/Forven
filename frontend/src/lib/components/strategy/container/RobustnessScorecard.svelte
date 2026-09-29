<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { stressVerdictText, type StressRow } from '$lib/utils/strategyContainer/evidence';
	import BulletBar from './BulletBar.svelte';

	export let rows: StressRow[] = [];
	export let loading = false;

	const dispatch = createEventDispatcher<{ open: { key: string } }>();

	const TONE: Record<StressRow['tone'], string> = { ok: 'text-[#3cc48f]', caution: 'text-[#e7b24a]', fail: 'text-[#e5574f]', idle: 'text-sc-ink3' };
</script>

<aside class="grid content-start gap-0 rounded-md border border-sc-line bg-sc-panel px-4 py-3.5" data-testid="robustness-scorecard">
	<div class="mb-1 flex items-baseline justify-between gap-2">
		<h2 class="m-0 text-[14px] font-semibold text-sc-ink">Robustness</h2>
		<button type="button" class="rounded-full border border-sc-line2 px-2.5 py-0.5 text-[11px] text-sc-ink2 hover:text-sc-ink" on:click={() => dispatch('open', { key: '' })}>Open tests</button>
	</div>
	{#if loading && rows.length === 0}
		<div class="py-4 text-[12px] text-sc-ink3">Loading the robustness evidence…</div>
	{/if}
	{#each rows as row (row.key)}
		<button type="button" class="grid grid-cols-[minmax(0,1fr)_104px_68px] items-center gap-2.5 border-b border-sc-line py-1.5 text-left last:border-b-0 hover:bg-sc-hover" data-testid={`scorecard-${row.key}`} on:click={() => dispatch('open', { key: row.key })}>
			<span class="min-w-0">
				<span class="text-[12px] font-medium text-sc-ink">{row.label}</span>
				<small class={`block text-[11px] ${row.weak || row.stale ? 'text-[#e7b24a]' : 'text-sc-ink3'}`}>{row.weak || row.stale ? row.evidence : row.value}</small>
			</span>
			<BulletBar bullet={row.bullet} tone={row.tone} />
			<span class={`text-right font-plex-cond text-[11px] font-semibold uppercase tracking-[0.06em] ${TONE[row.tone]}`}>{stressVerdictText(row)}</span>
		</button>
	{/each}
	<p class="m-0 mt-2 text-[12px] text-sc-ink3">Amber: passed its gate on thin or stale evidence, or advisory and below the usual bar. Tick = threshold, dot = result.</p>
</aside>
