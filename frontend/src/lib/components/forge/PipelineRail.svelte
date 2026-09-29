<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { PIPELINE_STAGES, type FlowSummary, type PipelineStage } from '$lib/utils/forge/flow';
	import { STATUS_META, TONE_BAR, type ForgeStatusKey } from '$lib/utils/forge/status';
	import type { BookTotals, LiveTotals } from '$lib/utils/forge/forward';
	import type { CauseCount } from '$lib/utils/forge/causes';
	import { fmtUsd, signClass } from '$lib/utils/strategyContainer/format';

	/** Strategies in each stage right now (the Forge's own rows). */
	export let counts: Record<PipelineStage, number>;
	/** Explainer status tallies per stage; empty while the explainer runs. */
	export let statusByStage: Record<PipelineStage, Partial<Record<ForgeStatusKey, number>>>;
	export let flow: FlowSummary | null = null;
	export let windowLabel = '24h';
	export let paper: BookTotals | null = null;
	export let live: LiveTotals | null = null;
	export let graveyardTotal: number | null = null;
	export let topCause: CauseCount | null = null;
	/** What the top cause was computed over, e.g. "last 24h" or "newest 500 loaded". */
	export let topCauseScope = '';
	/** The stage the table is filtered to ('all' for none, 'graveyard' for the graveyard). */
	export let selected = 'all';
	/** False until the gate explainer has answered once. */
	export let statusReady = false;

	const dispatch = createEventDispatcher<{ stage: string }>();

	const LABELS: Record<PipelineStage, string> = {
		quick_screen: 'Quick screen',
		gauntlet: 'Gauntlet',
		paper: 'Paper',
		live_graduated: 'Live',
	};
	const ORDER: ForgeStatusKey[] = ['awaiting_operator', 'blocked_merit', 'slot_contention', 'ready', 'in_flight', 'waiting_evidence', 'live', 'unknown'];

	// Takes the tallies as an argument so the template re-runs it when they change.
	function segments(byStage: typeof statusByStage, stage: PipelineStage) {
		const tally = byStage?.[stage] ?? {};
		const total = ORDER.reduce((sum, key) => sum + (tally[key] ?? 0), 0);
		return ORDER.filter((key) => (tally[key] ?? 0) > 0).map((key) => ({
			key,
			count: tally[key] ?? 0,
			share: total > 0 ? (tally[key] ?? 0) / total : 0,
			meta: STATUS_META[key],
		}));
	}

	function pct(rate: number | null): string {
		if (rate === null) return '';
		const value = rate * 100;
		return value > 0 && value < 1 ? '<1%' : `${Math.round(value)}%`;
	}

	function n(value: number | null | undefined): string {
		return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-US') : '—';
	}

	function select(key: string) {
		dispatch('stage', selected === key ? 'all' : key);
	}
</script>

<section class="overflow-hidden rounded-md border border-sc-line bg-sc-panel" data-testid="forge-pipeline">
	<div class="grid grid-cols-2 md:grid-cols-[repeat(4,minmax(0,1fr))_minmax(0,1.05fr)]">
		{#each PIPELINE_STAGES as stage, index (stage)}
			{@const f = flow?.byStage[stage] ?? null}
			{@const segs = segments(statusByStage, stage)}
			<button
				type="button"
				class="group relative grid min-w-0 content-start gap-1.5 border-b border-r border-sc-line px-4 pb-3 pt-3 text-left transition-colors hover:bg-sc-hover md:border-b-0 {selected === stage ? 'bg-sc-panel2 shadow-[inset_0_-2px_0_0_#eef1f5]' : ''}"
				aria-pressed={selected === stage}
				data-testid={`forge-stage-${stage}`}
				on:click={() => select(stage)}
			>
				<div class="flex items-center justify-between gap-2">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 group-hover:text-sc-ink2">{LABELS[stage]}</span>
					{#if index < PIPELINE_STAGES.length - 1 && f}
						<span
							class="hidden items-center gap-1 rounded-full border border-sc-line2 bg-sc-bg px-1.5 py-px font-plex-mono text-[10.5px] text-sc-ink2 md:inline-flex"
							title={`${f.promoted} promoted from ${LABELS[stage].toLowerCase()} to ${LABELS[PIPELINE_STAGES[index + 1]].toLowerCase()} in the last ${windowLabel}`}
						>{f.promoted}<span aria-hidden="true" class="text-sc-ink3">→</span></span>
					{/if}
				</div>
				<div class="flex items-baseline gap-1.5">
					<span class="font-plex-mono text-[26px] font-medium leading-none tracking-[-0.02em] text-sc-ink">{n(counts[stage])}</span>
					<span class="text-[11px] text-sc-ink3">now</span>
				</div>
				<div class="flex h-1.5 w-full overflow-hidden rounded-full bg-sc-line" title={segs.map((s) => `${s.count} ${s.meta.label.toLowerCase()}`).join(' · ')}>
					{#each segs as seg (seg.key)}
						<i class={`block h-full ${TONE_BAR[seg.meta.tone]}`} style={`width:${(seg.share * 100).toFixed(2)}%`}></i>
					{/each}
				</div>
				{#if segs.length > 0}
					<div class="truncate text-[11px] text-sc-ink2" title={segs.map((s) => `${s.count} ${s.meta.label.toLowerCase()}`).join(' · ')}>
						{#each segs.slice(0, 3) as seg, i (seg.key)}{#if i > 0}<span class="text-sc-ink4">{' · '}</span>{/if}<span class={seg.meta.tone === 'caution' ? 'text-[#e7b24a]' : seg.meta.tone === 'fail' ? 'text-[#f2956f]' : ''}>{seg.count} {seg.meta.short}</span>{/each}
					</div>
				{:else}
					<div class="text-[11px] text-sc-ink4">{counts[stage] === 0 ? 'Empty right now' : statusReady ? 'Not assessed' : 'Reading the gates…'}</div>
				{/if}
				<div class="mt-0.5 grid gap-0.5 border-t border-sc-line pt-1.5 text-[11px] leading-snug">
					{#if !f}
						<span class="text-sc-ink4">Loading flow…</span>
					{:else if stage === 'quick_screen'}
						<span class="text-sc-ink2"><b class="font-plex-mono font-medium text-sc-ink">{n(flow?.screened)}</b> screened{#if f.passRate !== null}<span class="text-sc-ink3"> · {pct(f.passRate)} pass</span>{/if}</span>
						<span class="text-sc-ink3"><b class="font-plex-mono font-medium text-[#f2956f]">{n(f.archived)}</b> to graveyard</span>
					{:else}
						<span class="text-sc-ink2"><b class="font-plex-mono font-medium text-sc-ink">{n(f.entered)}</b> {stage === 'live_graduated' ? 'graduated' : 'entered'}{#if stage !== 'live_graduated' && f.passRate !== null}<span class="text-sc-ink3"> · {pct(f.passRate)} pass</span>{/if}</span>
						<span class="text-sc-ink3"><b class="font-plex-mono font-medium {f.archived > 0 ? 'text-[#f2956f]' : 'text-sc-ink2'}">{n(f.archived)}</b> to graveyard</span>
					{/if}
					{#if stage === 'paper' && paper}
						<span class="text-sc-ink3" title="Realized P&L across every paper session, booked net on each strategy's simulated paper book. Not wallet money.">
							Paper book <b class={`font-plex-mono font-medium ${signClass(paper.pnlUsd)}`}>{fmtUsd(paper.pnlUsd, 0)}</b>{#if paper.open > 0} · {paper.open} open{/if}
						</span>
					{:else if stage === 'live_graduated' && live}
						<span class="text-sc-ink3" title="Realized live P&L in wallet dollars, net of recorded fees and funding. Not the wallet balance.">
							Live P&amp;L <b class={`font-plex-mono font-medium ${signClass(live.pnlUsd)}`}>{fmtUsd(live.pnlUsd, 2)}</b>{#if live.open > 0} · {live.open} in position{/if}
						</span>
					{/if}
				</div>
			</button>
		{/each}
		<button
			type="button"
			class="group grid min-w-0 content-start gap-1.5 bg-[repeating-linear-gradient(135deg,transparent_0_6px,rgba(255,255,255,0.012)_6px_12px)] px-4 pb-3 pt-3 text-left transition-colors hover:bg-sc-hover {selected === 'graveyard' ? 'bg-sc-panel2 shadow-[inset_0_-2px_0_0_#eef1f5]' : ''}"
			aria-pressed={selected === 'graveyard'}
			data-testid="forge-stage-graveyard"
			on:click={() => select('graveyard')}
		>
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 group-hover:text-sc-ink2">Graveyard</span>
			<div class="flex items-baseline gap-1.5">
				<span class="font-plex-mono text-[26px] font-medium leading-none tracking-[-0.02em] text-sc-ink2">{n(graveyardTotal)}</span>
				<span class="text-[11px] text-sc-ink3">total</span>
			</div>
			<div class="text-[11px] text-sc-ink2">
				{#if flow}
					<b class="font-plex-mono font-medium text-[#f2956f]">+{n(flow.archivedTotal)}</b> in the last {windowLabel}
				{:else}
					<span class="text-sc-ink4">Loading flow…</span>
				{/if}
			</div>
			<div class="mt-0.5 grid gap-0.5 border-t border-sc-line pt-1.5 text-[11px] leading-snug">
				{#if topCause}
					<span class="text-sc-ink3">Top cause{#if topCauseScope} · {topCauseScope}{/if}</span>
					<span class="truncate text-sc-ink2" title={`${topCause.label}${topCause.topDetail ? ` — mostly ${topCause.topDetail}` : ''}`}>
						{topCause.label} <b class="font-plex-mono font-medium text-sc-ink">{Math.round(topCause.share * 100)}%</b>
					</span>
				{:else}
					<span class="text-sc-ink4">Causes load with the graveyard</span>
				{/if}
			</div>
		</button>
	</div>
</section>
