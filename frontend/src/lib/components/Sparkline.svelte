<script lang="ts">
	export let data: number[] = [];
	export let width = 80;
	export let height = 24;

	function buildPath(values: number[], w: number, h: number): string {
		if (values.length < 2) return '';
		const min = Math.min(...values);
		const max = Math.max(...values);
		const range = max - min || 1;
		const stepX = w / (values.length - 1);
		const pad = 2;
		const usableH = h - pad * 2;

		return values
			.map((v, i) => {
				const x = i * stepX;
				const y = pad + usableH - ((v - min) / range) * usableH;
				return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(' ');
	}

	function getColor(values: number[]): string {
		if (values.length < 2) return '#666';
		return values[values.length - 1] >= values[0] ? '#4ade80' : '#f87171';
	}
</script>

{#if data.length >= 2}
	<svg {width} {height} viewBox="0 0 {width} {height}" class="inline-block align-middle">
		<path d={buildPath(data, width, height)} fill="none" stroke={getColor(data)} stroke-width="1.5" />
	</svg>
{:else}
	<span class="text-sc-ink4 text-[10px]">--</span>
{/if}
