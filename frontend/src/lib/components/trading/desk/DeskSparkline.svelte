<script lang="ts">
	/** Tiny cumulative P&L line with a zero baseline and an emphasized endpoint. */
	export let points: number[] = [];
	export let width = 76;
	export let height = 22;
	export let label = 'Cumulative P&L';

	$: last = points.length ? points[points.length - 1] : 0;
	$: lo = points.length ? Math.min(...points, 0) : 0;
	$: hi = points.length ? Math.max(...points, 0) : 0;
	$: span = hi - lo || 1;
	$: x = (i: number) => (points.length > 1 ? (i / (points.length - 1)) * (width - 4) + 2 : width / 2);
	$: y = (v: number) => height - 3 - ((v - lo) / span) * (height - 6);
	$: path = points.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
	$: stroke = last >= 0 ? '#139a9f' : '#e0663f';
</script>

<svg class="block" {width} {height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
	{#if points.length > 1}
		<line x1="2" x2={width - 2} y1={y(0)} y2={y(0)} stroke="#2a2f38" stroke-width="1" />
		<path d={path} fill="none" {stroke} stroke-width="1.6" stroke-linejoin="round" />
		<circle cx={x(points.length - 1)} cy={y(last)} r="2.2" fill={stroke} />
	{:else}
		<line x1="2" x2={width - 2} y1={height / 2} y2={height / 2} stroke="#2a2f38" stroke-width="1" />
	{/if}
</svg>
