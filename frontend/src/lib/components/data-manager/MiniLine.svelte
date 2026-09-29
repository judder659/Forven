<script lang="ts">
	// A neutral sparkline (no up/down colouring: shape only).
	export let values: number[] = [];
	export let width = 96;
	export let height = 22;
	export let label = '';

	$: finite = values.filter((v) => Number.isFinite(v));
	$: min = Math.min(...finite);
	$: max = Math.max(...finite);
	$: path = finite.length < 2
		? ''
		: finite
			.map((v, i) => {
				const x = (i / (finite.length - 1)) * width;
				const y = 2 + (height - 4) * (1 - (v - min) / (max - min || 1));
				return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(' ');
</script>

{#if path}
	<svg {width} {height} viewBox="0 0 {width} {height}" class="block" role="img" aria-label={label || 'trend'}>
		<path d={path} fill="none" stroke="#8b8b8b" stroke-width="1" />
	</svg>
{:else}
	<span class="block text-[10px] text-sc-ink4" style="width: {width}px">—</span>
{/if}
