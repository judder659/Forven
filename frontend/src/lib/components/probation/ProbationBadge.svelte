<script lang="ts">
	/** The striped amber PROBATION badge: a strategy trading live at reduced size. */
	import type { ProbationState } from '$lib/api/probation';

	export let state: ProbationState = 'active';
	export let sizePct: number | null = null;
	export let compact = false;

	$: label = state === 'paused' ? 'PROBATION · PAUSED' : state === 'demoting' ? 'PROBATION · DEMOTING' : 'PROBATION';
	$: title =
		state === 'demoting'
			? 'Being demoted to paper: no new entries, the open trade runs to its exit'
			: state === 'paused'
				? 'Live on probation, paused: no new entries'
				: `Live on probation at ${sizePct ?? '?'}% of normal size until its live trades confirm the backtest`;
</script>

<span
	class="inline-flex items-center gap-1 whitespace-nowrap rounded border border-amber-400/70 bg-[repeating-linear-gradient(135deg,rgba(251,191,36,0.28)_0_6px,rgba(251,191,36,0.08)_6px_12px)] font-plex-mono font-semibold tracking-wide text-amber-200 {compact ? 'px-1 py-0 text-[9px]' : 'px-1.5 py-0.5 text-[10px]'} {state === 'demoting' ? 'border-red-400/70 text-red-200' : ''}"
	{title}
	data-testid="probation-badge"
>{label}{#if sizePct !== null && state !== 'demoting'}<span class="font-normal text-amber-100/90">{Math.round(sizePct)}%</span>{/if}</span>
