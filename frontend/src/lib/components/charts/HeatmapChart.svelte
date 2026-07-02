<script lang="ts">
	import { onMount, onDestroy } from 'svelte';

	export let data: number[][] = [];
	export let xLabels: string[] = [];
	export let yLabels: string[] = [];
	export let width: number = 600;
	export let height: number = 400;
	export let colorScale: 'diverging' | 'sequential' = 'diverging';
	export let minValue: number | null = null;
	export let maxValue: number | null = null;
	export let valueFormat: (v: number) => string = (v) => v.toFixed(2);

	let canvas: HTMLCanvasElement;
	let tooltipEl: HTMLDivElement;
	let tooltipVisible = false;
	let tooltipX = 0;
	let tooltipY = 0;
	let tooltipContent = '';

	$: cellWidth = xLabels.length > 0 ? (width - 80) / xLabels.length : 0;
	$: cellHeight = yLabels.length > 0 ? (height - 60) / yLabels.length : 0;

	$: computedMin = minValue ?? Math.min(...data.flat().filter((v) => v !== null && !isNaN(v)));
	$: computedMax = maxValue ?? Math.max(...data.flat().filter((v) => v !== null && !isNaN(v)));

	function getColor(value: number | null): string {
		if (value === null || isNaN(value)) return '#1f2937';

		if (colorScale === 'diverging') {
			// Red (negative) -> White (zero) -> Green (positive)
			const absMax = Math.max(Math.abs(computedMin), Math.abs(computedMax));
			const normalized = absMax > 0 ? value / absMax : 0;

			if (normalized < 0) {
				const intensity = Math.min(1, Math.abs(normalized));
				const r = 239;
				const g = Math.round(68 + (255 - 68) * (1 - intensity));
				const b = Math.round(68 + (255 - 68) * (1 - intensity));
				return `rgb(${r}, ${g}, ${b})`;
			} else {
				const intensity = Math.min(1, normalized);
				const r = Math.round(74 + (255 - 74) * (1 - intensity));
				const g = 222;
				const b = Math.round(128 + (255 - 128) * (1 - intensity));
				return `rgb(${r}, ${g}, ${b})`;
			}
		} else {
			// Sequential: dark to bright blue
			const range = computedMax - computedMin;
			const normalized = range > 0 ? (value - computedMin) / range : 0;
			const intensity = Math.min(1, Math.max(0, normalized));
			const r = Math.round(31 + (59 - 31) * intensity);
			const g = Math.round(41 + (130 - 41) * intensity);
			const b = Math.round(55 + (246 - 55) * intensity);
			return `rgb(${r}, ${g}, ${b})`;
		}
	}

	function draw() {
		if (!canvas || data.length === 0) return;

		const ctx = canvas.getContext('2d');
		if (!ctx) return;

		ctx.clearRect(0, 0, width, height);

		const offsetX = 60;
		const offsetY = 30;

		// Draw cells
		for (let row = 0; row < data.length; row++) {
			for (let col = 0; col < data[row].length; col++) {
				const value = data[row][col];
				ctx.fillStyle = getColor(value);
				ctx.fillRect(offsetX + col * cellWidth, offsetY + row * cellHeight, cellWidth - 1, cellHeight - 1);

				// Draw value text if cell is big enough
				if (cellWidth > 30 && cellHeight > 20 && value !== null && !isNaN(value)) {
					ctx.fillStyle = colorScale === 'diverging' && Math.abs(value) > (computedMax - computedMin) * 0.3
						? '#ffffff'
						: '#9ca3af';
					ctx.font = '10px monospace';
					ctx.textAlign = 'center';
					ctx.textBaseline = 'middle';
					ctx.fillText(
						valueFormat(value),
						offsetX + col * cellWidth + cellWidth / 2,
						offsetY + row * cellHeight + cellHeight / 2
					);
				}
			}
		}

		// Draw x-axis labels
		ctx.fillStyle = '#9ca3af';
		ctx.font = '10px monospace';
		ctx.textAlign = 'center';
		ctx.textBaseline = 'top';
		for (let col = 0; col < xLabels.length; col++) {
			ctx.fillText(xLabels[col], offsetX + col * cellWidth + cellWidth / 2, height - 25);
		}

		// Draw y-axis labels
		ctx.textAlign = 'right';
		ctx.textBaseline = 'middle';
		for (let row = 0; row < yLabels.length; row++) {
			ctx.fillText(yLabels[row], offsetX - 8, offsetY + row * cellHeight + cellHeight / 2);
		}
	}

	function handleMouseMove(event: MouseEvent) {
		const rect = canvas.getBoundingClientRect();
		const x = event.clientX - rect.left;
		const y = event.clientY - rect.top;

		const offsetX = 60;
		const offsetY = 30;

		const col = Math.floor((x - offsetX) / cellWidth);
		const row = Math.floor((y - offsetY) / cellHeight);

		if (col >= 0 && col < xLabels.length && row >= 0 && row < yLabels.length) {
			const value = data[row]?.[col];
			if (value !== null && value !== undefined && !isNaN(value)) {
				tooltipContent = `${yLabels[row]} / ${xLabels[col]}: ${valueFormat(value)}`;
				tooltipX = event.clientX + 10;
				tooltipY = event.clientY + 10;
				tooltipVisible = true;
				return;
			}
		}
		tooltipVisible = false;
	}

	function handleMouseLeave() {
		tooltipVisible = false;
	}

	// Redraw whenever any input draw() reads changes. Svelte only tracks variables
	// referenced directly in a reactive block (not inside the called function), so list
	// them here — otherwise a size/scale/label change without a `data` change leaves a
	// stale canvas.
	$: if (canvas && data.length > 0) {
		void [data, width, height, cellWidth, cellHeight, computedMin, computedMax, colorScale, valueFormat, xLabels, yLabels];
		draw();
	}

	onMount(() => {
		draw();
	});
</script>

<div class="heatmap-container relative">
	<canvas
		bind:this={canvas}
		{width}
		{height}
		on:mousemove={handleMouseMove}
		on:mouseleave={handleMouseLeave}
		class="cursor-crosshair"
	></canvas>

	{#if tooltipVisible}
		<div
			bind:this={tooltipEl}
			class="fixed z-50 px-2 py-1 text-xs bg-[#111] border border-[#333] text-white pointer-events-none"
			style="left: {tooltipX}px; top: {tooltipY}px;"
		>
			{tooltipContent}
		</div>
	{/if}
</div>

<style>
	.heatmap-container {
		display: inline-block;
	}
</style>
