<script lang="ts">
	import { onMount } from 'svelte';

	export let bins: number[] = [];
	export let counts: number[] = [];
	export let stats: {
		mean?: number;
		std?: number;
		skewness?: number;
		kurtosis?: number;
		min?: number;
		max?: number;
		percentile_5?: number;
		percentile_95?: number;
	} = {};
	export let width: number = 500;
	export let height: number = 300;
	export let title: string = '';
	export let xLabel: string = '';
	export let showStats: boolean = true;
	export let colorBySign: boolean = true;

	let canvas: HTMLCanvasElement;
	let tooltipVisible = false;
	let tooltipX = 0;
	let tooltipY = 0;
	let tooltipContent = '';

	$: maxCount = Math.max(...counts, 1);
	$: binWidth = bins.length > 1 ? bins[1] - bins[0] : 1;

	function getBarColor(binValue: number): string {
		if (!colorBySign) return '#3b82f6';
		return binValue >= 0 ? '#4ade80' : '#f87171';
	}

	function draw() {
		if (!canvas || bins.length === 0 || counts.length === 0) return;

		const ctx = canvas.getContext('2d');
		if (!ctx) return;

		ctx.clearRect(0, 0, width, height);

		const padding = { top: 40, right: 20, bottom: 50, left: 60 };
		const chartWidth = width - padding.left - padding.right;
		const chartHeight = height - padding.top - padding.bottom;

		// Draw title
		if (title) {
			ctx.fillStyle = '#ffffff';
			ctx.font = 'bold 12px monospace';
			ctx.textAlign = 'center';
			ctx.fillText(title, width / 2, 20);
		}

		// Calculate bar dimensions
		const barWidth = chartWidth / bins.length;

		// Find zero position for x-axis
		const minBin = Math.min(...bins);
		const maxBin = Math.max(...bins) + binWidth;
		const range = maxBin - minBin;
		const zeroX = minBin < 0 && maxBin > 0 ? padding.left + ((-minBin) / range) * chartWidth : null;

		// Draw histogram bars
		bins.forEach((bin, i) => {
			const count = counts[i] || 0;
			const barHeight = (count / maxCount) * chartHeight;
			const x = padding.left + i * barWidth;
			const y = padding.top + chartHeight - barHeight;

			ctx.fillStyle = getBarColor(bin + binWidth / 2);
			ctx.fillRect(x, y, barWidth - 1, barHeight);
		});

		// Draw zero line
		if (zeroX !== null) {
			ctx.strokeStyle = '#9ca3af';
			ctx.lineWidth = 1;
			ctx.setLineDash([4, 4]);
			ctx.beginPath();
			ctx.moveTo(zeroX, padding.top);
			ctx.lineTo(zeroX, padding.top + chartHeight);
			ctx.stroke();
			ctx.setLineDash([]);
		}

		// Draw mean line
		if (stats.mean !== undefined) {
			const meanX = padding.left + ((stats.mean - minBin) / range) * chartWidth;
			if (meanX >= padding.left && meanX <= padding.left + chartWidth) {
				ctx.strokeStyle = '#fbbf24';
				ctx.lineWidth = 2;
				ctx.beginPath();
				ctx.moveTo(meanX, padding.top);
				ctx.lineTo(meanX, padding.top + chartHeight);
				ctx.stroke();

				// Label
				ctx.fillStyle = '#fbbf24';
				ctx.font = '10px monospace';
				ctx.textAlign = 'center';
				ctx.fillText('mean', meanX, padding.top - 5);
			}
		}

		// Draw x-axis
		ctx.strokeStyle = '#4b5563';
		ctx.lineWidth = 1;
		ctx.beginPath();
		ctx.moveTo(padding.left, padding.top + chartHeight);
		ctx.lineTo(padding.left + chartWidth, padding.top + chartHeight);
		ctx.stroke();

		// Draw x-axis labels
		ctx.fillStyle = '#9ca3af';
		ctx.font = '10px monospace';
		ctx.textAlign = 'center';
		ctx.textBaseline = 'top';

		const labelCount = Math.min(5, bins.length);
		const labelStep = Math.floor(bins.length / labelCount);
		for (let i = 0; i < bins.length; i += labelStep) {
			const x = padding.left + i * barWidth + barWidth / 2;
			ctx.fillText(bins[i].toFixed(2), x, padding.top + chartHeight + 8);
		}

		// Draw x-axis label
		if (xLabel) {
			ctx.fillText(xLabel, width / 2, height - 10);
		}

		// Draw y-axis labels
		ctx.textAlign = 'right';
		ctx.textBaseline = 'middle';
		for (let i = 0; i <= 4; i++) {
			const y = padding.top + chartHeight - (i / 4) * chartHeight;
			const value = Math.round((i / 4) * maxCount);
			ctx.fillText(value.toString(), padding.left - 8, y);
		}

		// Draw stats box
		if (showStats && Object.keys(stats).length > 0) {
			const statsX = padding.left + chartWidth - 100;
			const statsY = padding.top + 10;

			ctx.fillStyle = 'rgba(31, 41, 55, 0.9)';
			ctx.fillRect(statsX, statsY, 95, 70);
			ctx.strokeStyle = '#374151';
			ctx.strokeRect(statsX, statsY, 95, 70);

			ctx.fillStyle = '#9ca3af';
			ctx.font = '9px monospace';
			ctx.textAlign = 'left';

			let line = 0;
			if (stats.mean !== undefined) {
				ctx.fillText(`Mean: ${stats.mean.toFixed(3)}`, statsX + 5, statsY + 12 + line * 12);
				line++;
			}
			if (stats.std !== undefined) {
				ctx.fillText(`Std: ${stats.std.toFixed(3)}`, statsX + 5, statsY + 12 + line * 12);
				line++;
			}
			if (stats.skewness !== undefined) {
				ctx.fillText(`Skew: ${stats.skewness.toFixed(3)}`, statsX + 5, statsY + 12 + line * 12);
				line++;
			}
			if (stats.kurtosis !== undefined) {
				ctx.fillText(`Kurt: ${stats.kurtosis.toFixed(3)}`, statsX + 5, statsY + 12 + line * 12);
				line++;
			}
		}
	}

	function handleMouseMove(event: MouseEvent) {
		const rect = canvas.getBoundingClientRect();
		const x = event.clientX - rect.left;
		const y = event.clientY - rect.top;

		const padding = { top: 40, right: 20, bottom: 50, left: 60 };
		const chartWidth = width - padding.left - padding.right;
		const chartHeight = height - padding.top - padding.bottom;

		const barWidth = chartWidth / bins.length;
		const barIndex = Math.floor((x - padding.left) / barWidth);

		if (barIndex >= 0 && barIndex < bins.length && y >= padding.top && y <= padding.top + chartHeight) {
			const bin = bins[barIndex];
			const count = counts[barIndex];
			tooltipContent = `[${bin.toFixed(2)}, ${(bin + binWidth).toFixed(2)}): ${count}`;
			tooltipX = event.clientX + 10;
			tooltipY = event.clientY + 10;
			tooltipVisible = true;
			return;
		}
		tooltipVisible = false;
	}

	function handleMouseLeave() {
		tooltipVisible = false;
	}

	$: if (canvas && bins.length > 0) {
		draw();
	}

	onMount(() => {
		draw();
	});
</script>

<div class="distribution-chart-container relative">
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
	.distribution-chart-container {
		display: inline-block;
	}
</style>
