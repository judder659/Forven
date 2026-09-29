<script lang="ts">
	// A server SLA state as colour + word, optionally with "behind / allowed".
	import type { SlaAssessment, SlaState } from '$lib/api/dataManagerTypes';
	import { lagCaption, STATE_HELP, STATE_LABEL, stateChipClass, stateFillClass } from './format';

	export let state: SlaState;
	export let sla: Pick<SlaAssessment, 'lag_seconds' | 'allowed_seconds'> | null = null;
	/** Show the "5.5 h / 2 h" caption next to the chip. */
	export let caption = false;

	$: lag = sla ? lagCaption(sla) : '';
	$: title = `${STATE_HELP[state]}${lag ? ` Behind / allowed: ${lag}.` : ''}`;
</script>

<span class="inline-flex items-center gap-1.5 whitespace-nowrap" {title}>
	<span class="inline-flex items-center gap-1 border px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {stateChipClass(state)}">
		<span class="h-1.5 w-1.5 shrink-0 {stateFillClass(state)}" aria-hidden="true"></span>
		{STATE_LABEL[state]}
	</span>
	{#if caption && lag}<span class="font-mono text-[10px] tabular-nums text-sc-ink2">{lag}</span>{/if}
</span>
