<script lang="ts">
	import { onMount } from 'svelte';

	export let data: Array<{ label: string; value: number }> = [];
	export let width: number = 400;
	export let height: number = 300;
	export let horizontal: boolean = true;
	export let showValues: boolean = true;
	export let sortByValue: boolean = true;
	export let colorBySign: boolean = true;
	export let valueFormat: (v: number) => string = (v) => v.toFixed(2);
	export let maxBars: number = 20;

	let canvas: HTMLCanvasElement;
	let tooltipVisible = false;
	let tooltipX = 0;
	let tooltipY = 0;
	let tooltipContent = '';

	$: sortedData = sortByValue
		? [...data].sort((a, b) => b.value - a.value).slice(0, maxBars)
		: data.slice(0, maxBars);

	$: maxAbsValue = Math.max(...sortedData.map((d) => Math.abs(d.value)), 0.001);

	function getBarColor(value: number): string {
		if (!colorBySign) return '#3b82f6';
		return value >= 0 ? '#4ade80' : '#f87171';
	}

	function draw() {
		if (!canvas || sortedData.length === 0) return;

		const ctx = canvas.getContext('2d');
		if (!ctx) return;

		ctx.clearRect(0, 0, width, height);

		const padding = { top: 20, right: 20, bottom: 30, left: 120 };
		const chartWidth = width - padding.left - padding.right;
		const chartHeight = height - padding.top - padding.bottom;

		if (horizontal) {
			const barHeight = Math.min(25, (chartHeight - (sortedData.length - 1) * 4) / sortedData.length);
			const totalBarsHeight = sortedData.length * barHeight + (sortedData.length - 1) * 4;
			const startY = padding.top + (chartHeight - totalBarsHeight) / 2;

			// Draw zero line if we have negative values
			const hasNegative = sortedData.some((d) => d.value < 0);
			const zeroX = hasNegative ? padding.left + chartWidth / 2 : padding.left;

			if (hasNegative) {
				ctx.strokeStyle = '#4b5563';
				ctx.beginPath();
				ctx.moveTo(zeroX, padding.top);
				ctx.lineTo(zeroX, height - padding.bottom);
				ctx.stroke();
			}

			sortedData.forEach((item, i) => {
				const y = startY + i * (barHeight + 4);
				const barWidth = (Math.abs(item.value) / maxAbsValue) * (chartWidth / (hasNegative ? 2 : 1));

				// Draw bar
				ctx.fillStyle = getBarColor(item.value);
				if (item.value >= 0) {
					ctx.fillRect(zeroX, y, barWidth, barHeight);
				} else {
					ctx.fillRect(zeroX - barWidth, y, barWidth, barHeight);
				}

				// Draw label
				ctx.fillStyle = '#9ca3af';
				ctx.font = '11px monospace';
				ctx.textAlign = 'right';
				ctx.textBaseline = 'middle';
				const labelText = item.label.length > 15 ? item.label.slice(0, 15) + '...' : item.label;
				ctx.fillText(labelText, padding.left - 8, y + barHeight / 2);

				// Draw value
				if (showValues) {
					ctx.fillStyle = '#ffffff';
					ctx.textAlign = item.value >= 0 ? 'left' : 'right';
					const valueX = item.value >= 0 ? zeroX + barWidth + 6 : zeroX - barWidth - 6;
					ctx.fillText(valueFormat(item.value), valueX, y + barHeight / 2);
				}
			});
		} else {
			// Vertical bars
			const barWidth = Math.min(40, (chartWidth - (sortedData.length - 1) * 4) / sortedData.length);
			const totalBarsWidth = sortedData.length * barWidth + (sortedData.length - 1) * 4;
			const startX = padding.left + (chartWidth - totalBarsWidth) / 2;

			sortedData.forEach((item, i) => {
				const x = startX + i * (barWidth + 4);
				const barHeight = (Math.abs(item.value) / maxAbsValue) * chartHeight;

				// Draw bar
				ctx.fillStyle = getBarColor(item.value);
				ctx.fillRect(x, height - padding.bottom - barHeight, barWidth, barHeight);

				// Draw label
				ctx.fillStyle = '#9ca3af';
				ctx.font = '10px monospace';
				ctx.textAlign = 'center';
				ctx.textBaseline = 'top';
				ctx.save();
				ctx.translate(x + barWidth / 2, height - padding.bottom + 8);
				ctx.rotate(-Math.PI / 4);
				ctx.textAlign = 'right';
				const labelText = item.label.length > 10 ? item.label.slice(0, 10) + '...' : item.label;
				ctx.fillText(labelText, 0, 0);
				ctx.restore();
			});
		}
	}

	function handleMouseMove(event: MouseEvent) {
		const rect = canvas.getBoundingClientRect();
		const x = event.clientX - rect.left;
		const y = event.clientY - rect.top;

		const padding = { top: 20, right: 20, bottom: 30, left: 120 };
		const chartHeight = height - padding.top - padding.bottom;

		if (horizontal) {
			const barHeight = Math.min(25, (chartHeight - (sortedData.length - 1) * 4) / sortedData.length);
			const totalBarsHeight = sortedData.length * barHeight + (sortedData.length - 1) * 4;
			const startY = padding.top + (chartHeight - totalBarsHeight) / 2;

			const barIndex = Math.floor((y - startY) / (barHeight + 4));
			if (barIndex >= 0 && barIndex < sortedData.length && x >= padding.left) {
				const item = sortedData[barIndex];
				tooltipContent = `${item.label}: ${valueFormat(item.value)}`;
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

	$: if (canvas && sortedData.length > 0) {
		draw();
	}

	onMount(() => {
		draw();
	});
</script>

<div class="bar-chart-container relative">
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
			class="fixed z-50 px-2 py-1 text-xs bg-[#111] border border-[#333] text-white pointer-events-none"
			style="left: {tooltipX}px; top: {tooltipY}px;"
		>
			{tooltipContent}
		</div>
	{/if}
</div>

<style>
	.bar-chart-container {
		display: inline-block;
	}
</style>
