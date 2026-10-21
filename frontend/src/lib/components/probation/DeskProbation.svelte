<script lang="ts">
	/**
	 * The live desk's probation strip: every strategy trading live at reduced
	 * size (loud, striped) and the paper strategies ready to start probation.
	 * Start, size and demote live on each strategy's page; this links there.
	 */
	import type { ProbationOverview } from '$lib/api/probation';
	import ProbationBadge from './ProbationBadge.svelte';

	export let overview: ProbationOverview | null = null;

	let showReady = false;

	$: rows = overview?.strategies ?? [];
	$: readyAll = overview?.ready?.strategies ?? [];
	$: ready = readyAll.filter((r) => r.eligible);
	$: graduateAt = overview?.ladder.graduate_trades ?? 10;
	$: auto = overview?.automation;

	const href = (id: string) => `/lab/strategy/${encodeURIComponent(id)}`;
</script>

{#if overview && (rows.length || ready.length)}
	<section class="grid gap-2" data-testid="desk-probation">
		{#if rows.length}
			<div class="grid gap-1.5 rounded-lg border-2 border-amber-400/70 bg-[repeating-linear-gradient(135deg,rgba(251,191,36,0.10)_0_14px,rgba(251,191,36,0.03)_14px_28px)] px-3 py-2">
				<div class="flex flex-wrap items-center gap-2 text-[12px]">
					<ProbationBadge sizePct={null} />
					<span class="font-semibold text-amber-100">{rows.length} live strateg{rows.length === 1 ? 'y' : 'ies'} on probation (real money, reduced size)</span>
					<span class="text-sc-ink3">· {rows.length} of {overview.max_strategies} slots · demotion {auto?.auto_demote ? 'automatic' : 'by hand'}</span>
				</div>
				{#each rows as row (row.strategy_id)}
					<a href={href(row.strategy_id)} class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded px-1 py-0.5 text-[12px] hover:bg-amber-400/10" data-testid="desk-probation-row">
						<span class="flex min-w-0 items-center gap-2">
							<ProbationBadge state={row.state} sizePct={row.size_pct} compact />
							<span class="font-plex-mono text-sc-ink">{row.display_id}</span>
							<span class="truncate text-sc-ink2">{row.name} · {row.symbol} {row.timeframe}</span>
						</span>
						<span class="flex items-center gap-3 font-plex-mono text-[11px]">
							<span class="text-sc-ink2">{row.live_trades}/{graduateAt} trades</span>
							<span class={row.live_return_pct < 0 ? 'text-red-300' : 'text-sc-ink'}>{row.live_return_pct > 0 ? '+' : ''}{row.live_return_pct.toFixed(2)}%</span>
							{#if row.triggers.length}<span class="text-red-300" title={row.triggers.map((t) => t.label).join(', ')}>⚠ {row.triggers.length}</span>{/if}
						</span>
					</a>
				{/each}
			</div>
		{/if}
		{#if ready.length}
			<div class="grid gap-1 rounded-lg border border-sc-line bg-sc-panel px-3 py-2 text-[12px]">
				<div class="flex flex-wrap items-center gap-2">
					<span class="rounded border border-emerald-600/60 bg-emerald-600/15 px-1.5 py-0.5 text-[10px] text-emerald-200">READY</span>
					<span class="text-sc-ink">{ready.length} paper strateg{ready.length === 1 ? 'y is' : 'ies are'} ready for probation</span>
					<span class="text-sc-ink3">{auto?.auto_entry ? '· they start by themselves at the next check' : '· start each one on its page'}</span>
					<button type="button" class="text-[11px] text-sc-ink3 underline hover:text-sc-ink" on:click={() => (showReady = !showReady)}>{showReady ? 'Hide' : 'Show'}</button>
				</div>
				{#if showReady}
					{#each ready as row (row.strategy_id)}
						<a href={href(row.strategy_id)} class="flex gap-2 rounded px-1 py-0.5 hover:bg-sc-hover" data-testid="desk-probation-ready">
							<span class="font-plex-mono text-sc-ink">{row.display_id}</span>
							<span class="truncate text-sc-ink2">{row.name} · {row.symbol} {row.timeframe}</span>
							<span class="ml-auto text-sc-ink3">{row.confirmation.matched} paper trade{row.confirmation.matched === 1 ? '' : 's'} confirmed</span>
						</a>
					{/each}
				{/if}
			</div>
		{/if}
	</section>
{/if}
