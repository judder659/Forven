<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { stressVerdictText, type StressRow } from '$lib/utils/strategyContainer/evidence';
	import BulletBar from './BulletBar.svelte';

	export let rows: StressRow[] = [];
	export let loading = false;

	const dispatch = createEventDispatcher<{ open: { key: string } }>();

	const TONE: Record<StressRow['tone'], string> = { ok: 'text-[#3cc48f]', caution: 'text-[#e7b24a]', fail: 'text-[#e5574f]', idle: 'text-[#666]' };
</script>

<aside class="grid content-start gap-0 border border-[#1d1d1d] bg-[#090909] p-4" data-testid="robustness-scorecard">
	<div class="mb-1 flex items-baseline justify-between gap-2">
		<h2 class="m-0 text-[13px] font-semibold text-white">Robustness</h2>
		<button type="button" class="rounded-full border border-[#2a2f38] px-2.5 py-0.5 text-[11px] text-[#aab1bc] hover:text-white" on:click={() => dispatch('open', { key: '' })}>Open tests</button>
	</div>
	{#if loading && rows.length === 0}
		<div class="py-4 text-[12px] text-[#666]">Loading the robustness evidence…</div>
	{/if}
	{#each rows as row (row.key)}
		<button type="button" class="grid grid-cols-[minmax(0,1fr)_104px_68px] items-center gap-2.5 border-b border-[#1d1d1d] py-1.5 text-left last:border-b-0 hover:bg-[#0d0d0d]" data-testid={`scorecard-${row.key}`} on:click={() => dispatch('open', { key: row.key })}>
			<span class="min-w-0">
				<span class="text-[12px] font-medium text-white">{row.label}</span>
				<small class={`block text-[11px] ${row.weak || row.stale ? 'text-[#e7b24a]' : 'text-[#666]'}`}>{row.weak || row.stale ? row.evidence : row.value}</small>
			</span>
			<BulletBar bullet={row.bullet} tone={row.tone} />
			<span class={`text-right text-[10.5px] font-semibold uppercase tracking-[0.06em] ${TONE[row.tone]}`}>{stressVerdictText(row)}</span>
		</button>
	{/each}
	<p class="m-0 mt-2 text-[11px] text-[#666]">Amber: passed its gate on thin or stale evidence, or advisory and below the usual bar. Tick = threshold, dot = result.</p>
</aside>
