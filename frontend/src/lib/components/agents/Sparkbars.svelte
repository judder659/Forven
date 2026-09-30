<script lang="ts">
	import type { FleetBucket } from '$lib/api/agentsHub';

	/** Oldest first; one bar per bucket, stacked succeeded / blocked / failed. */
	export let buckets: FleetBucket[] = [];
	export let height = 22;
	export let barWidth = 4;
	export let gap = 1;
	/** Accessible summary, e.g. "129 runs in the last 24 hours". */
	export let label = '';

	$: width = Math.max(1, buckets.length * (barWidth + gap) - gap);
	$: peak = Math.max(1, ...buckets.map((bucket) => bucket.ok + bucket.failed + bucket.blocked));

	function scaled(count: number): number {
		if (count <= 0) return 0;
		// A single run still shows as a visible stub.
		return Math.max(2, (count / peak) * height);
	}
</script>

<svg {width} {height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} class="block shrink-0 overflow-visible">
	{#each buckets as bucket, index (index)}
		{@const x = index * (barWidth + gap)}
		{@const total = bucket.ok + bucket.failed + bucket.blocked}
		{#if total === 0}
			<rect {x} y={height - 1} width={barWidth} height="1" class="fill-sc-line2" />
		{:else}
			{@const full = scaled(total)}
			{@const failedH = (bucket.failed / total) * full}
			{@const blockedH = (bucket.blocked / total) * full}
			{@const okH = full - failedH - blockedH}
			<rect {x} y={height - full} width={barWidth} height={okH} rx="0.5" class="fill-[#3cc48f]/70" />
			<rect {x} y={height - failedH - blockedH} width={barWidth} height={blockedH} class="fill-[#e7b24a]" />
			<rect {x} y={height - failedH} width={barWidth} height={failedH} class="fill-[#e5574f]" />
		{/if}
	{/each}
</svg>
