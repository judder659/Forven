<script lang="ts">
	// Share of series in each SLA state; every segment names its count in its title.
	import type { SlaState } from '$lib/api/dataManagerTypes';
	import { STATE_LABEL, STATES, stateFillClass } from './format';

	export let counts: Partial<Record<SlaState, number>>;
	export let height = 6;

	$: total = STATES.reduce((sum, state) => sum + (counts[state] ?? 0), 0);
	$: segments = STATES.filter((state) => (counts[state] ?? 0) > 0).map((state) => ({ state, n: counts[state] ?? 0 }));
</script>

<div class="flex w-full gap-px overflow-hidden bg-[#111]" style="height: {height}px" role="img"
	aria-label={segments.length ? segments.map((s) => `${s.n} ${STATE_LABEL[s.state].toLowerCase()}`).join(', ') : 'no series'}>
	{#each segments as segment (segment.state)}
		<div class="h-full min-w-[2px] {stateFillClass(segment.state)}" style="width: {(segment.n / total) * 100}%"
			title="{segment.n.toLocaleString('en-US')} {STATE_LABEL[segment.state].toLowerCase()}"></div>
	{/each}
</div>
