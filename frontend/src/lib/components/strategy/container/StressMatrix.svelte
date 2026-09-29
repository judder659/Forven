<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { stressVerdictText, type StressRow } from '$lib/utils/strategyContainer/evidence';
	import BulletBar from './BulletBar.svelte';

	export let rows: StressRow[] = [];
	export let composite: number | null = null;
	export let floor: number | null = null;
	export let loading = false;

	const dispatch = createEventDispatcher<{ select: { key: string } }>();
	const TONE: Record<StressRow['tone'], string> = { ok: 'text-[#3cc48f]', caution: 'text-[#e7b24a]', fail: 'text-[#e5574f]', idle: 'text-sc-ink3' };
	$: staleRows = rows.filter((row) => row.stale);
</script>

<article class="grid content-start gap-3 rounded-md border border-sc-line bg-sc-panel px-4 py-3.5" id="stress-matrix" data-testid="stress-matrix">
	<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
		<div>
			<h2 class="m-0 text-[14px] font-semibold text-sc-ink">Stress matrix</h2>
			<div class="text-[12px] text-sc-ink3">Each test asks one question. The verdict is the configured gate; the evidence column says how much weight the verdict can carry.</div>
		</div>
		{#if composite !== null}
			<span class="text-[11px] text-sc-ink2" data-testid="stress-composite">Gauntlet composite <b class="text-sc-ink">{composite.toFixed(1)}</b> / 100{floor !== null ? ` · floor ${floor.toFixed(0)}` : ''}</span>
		{/if}
	</div>
	{#if staleRows.length}
		<div class="border border-yellow-900 bg-yellow-500/5 px-2.5 py-2 text-[11px] text-yellow-400" data-testid="stress-stale-warning">
			Params changed since {staleRows.map((row) => row.label).join(', ')} ran — {staleRows.length === 1 ? 'that verdict describes' : 'those verdicts describe'} an older version of this strategy. Rerun before relying on {staleRows.length === 1 ? 'it' : 'them'}.
		</div>
	{/if}
	{#if loading && rows.length === 0}
		<div class="py-4 text-[12px] text-sc-ink3">Loading the robustness evidence…</div>
	{:else}
		<div class="overflow-x-auto">
			<table class="w-full min-w-[860px] border-collapse text-[12px]">
				<thead>
					<tr class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3">
						<th class="px-2.5 py-1.5 text-left font-normal">Test</th>
						<th class="px-2.5 py-1.5 text-left font-normal">Question</th>
						<th class="px-2.5 py-1.5 text-right font-normal">Result</th>
						<th class="px-2.5 py-1.5 text-left font-normal">Against threshold</th>
						<th class="px-2.5 py-1.5 text-left font-normal">Verdict</th>
						<th class="px-2.5 py-1.5 text-left font-normal">Evidence</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as row (row.key)}
						<tr class="border-t border-sc-line hover:bg-sc-hover" data-testid={`stress-row-${row.key}`}>
							<td class="px-2.5 py-2 text-left">
								<button type="button" class="text-left text-[12px] text-sc-ink underline decoration-sc-line2 underline-offset-2 hover:decoration-sc-ink" on:click={() => dispatch('select', { key: row.key })}>{row.label}</button>
							</td>
							<td class="min-w-[200px] px-2.5 py-2 text-left text-sc-ink3">{row.question}</td>
							<td class="whitespace-nowrap px-2.5 py-2 text-right font-plex-mono tabular-nums text-sc-ink">{row.value}</td>
							<td class="min-w-[170px] px-2.5 py-2 text-left">
								<div class="grid gap-1"><BulletBar bullet={row.bullet} tone={row.tone} /><span class="text-[12px] text-sc-ink3">{row.thresholdText}</span></div>
							</td>
							<td class="whitespace-nowrap px-2.5 py-2 text-left">
								<span class={`font-plex-cond text-[11px] font-semibold uppercase tracking-[0.06em] ${TONE[row.tone]}`}>{stressVerdictText(row)}</span>
								{#if row.stale}
									<span class="ml-1 rounded-full border border-yellow-900 px-1.5 font-plex-cond text-[10px] uppercase text-yellow-400" data-testid={`stress-stale-${row.key}`} title="Params changed after this test ran">Stale</span>
								{/if}
							</td>
							<td class={`min-w-[200px] px-2.5 py-2 text-left text-[11px] ${row.weak || row.stale ? 'text-[#e7b24a]' : 'text-sc-ink3'}`}>{row.evidence}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</article>
