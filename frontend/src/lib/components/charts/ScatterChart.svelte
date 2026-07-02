<script context="module" lang="ts">
	export interface Point {
		x: number;
		y: number;
		label?: string;
		color?: string;
		size?: number;
		type?: 'circle' | 'diamond' | 'star';
	}
</script>

<script lang="ts">
	import { onMount } from 'svelte';

	export let points: Point[] = [];
	export let width: number = 500;
	export let height: number = 400;
	export let xLabel: string = 'Risk (Volatility %)';
	export let yLabel: string = 'Return (%)';
	export let highlights: Point[] = [];
	export let showLine: boolean = false;

	let canvas: HTMLCanvasElement;
	let tooltipVisible = false;
	let tooltipX = 0;
	let tooltipY = 0;
	let tooltipContent = '';

	$: allPoints = [...points, ...highlights];
	$: xMin = Math.min(...allPoints.map((p) => p.x)) * 0.95;
	$: xMax = Math.max(...allPoints.map((p) => p.x)) * 1.05;
	$: yMin = Math.min(...allPoints.map((p) => p.y)) * 0.95;
	$: yMax = Math.max(...allPoints.map((p) => p.y)) * 1.05;

	function scaleX(x: number, chartWidth: number, offsetX: number): number {
		return offsetX + ((x - xMin) / (xMax - xMin)) * chartWidth;
	}

	function scaleY(y: number, chartHeight: number, offsetY: number): number {
		return offsetY + chartHeight - ((y - yMin) / (yMax - yMin)) * chartHeight;
	}

	function drawPoint(ctx: CanvasRenderingContext2D, x: number, y: number, point: Point) {
		const size = point.size || 4;
		const color = point.color || '#3b82f6';
		const type = point.type || 'circle';

		ctx.fillStyle = color;
		ctx.strokeStyle = color;

		if (type === 'circle') {
			ctx.beginPath();
			ctx.arc(x, y, size, 0, Math.PI * 2);
			ctx.fill();
		} else if (type === 'diamond') {
			ctx.beginPath();
			ctx.moveTo(x, y - size);
			ctx.lineTo(x + size, y);
			ctx.lineTo(x, y + size);
			ctx.lineTo(x - size, y);
			ctx.closePath();
			ctx.fill();
		} else if (type === 'star') {
			const spikes = 5;
			const outerRadius = size * 1.5;
			const innerRadius = size * 0.6;
			ctx.beginPath();
			for (let i = 0; i < spikes * 2; i++) {
				const radius = i % 2 === 0 ? outerRadius : innerRadius;
				const angle = (i * Math.PI) / spikes - Math.PI / 2;
				const px = x + Math.cos(angle) * radius;
				const py = y + Math.sin(angle) * radius;
				if (i === 0) ctx.moveTo(px, py);
				else ctx.lineTo(px, py);
			}
			ctx.closePath();
			ctx.fill();
		}
	}

	function draw() {
		if (!canvas || points.length === 0) return;

		const ctx = canvas.getContext('2d');
		if (!ctx) return;

		ctx.clearRect(0, 0, width, height);

		const padding = { top: 30, right: 30, bottom: 50, left: 60 };
		const chartWidth = width - padding.left - padding.right;
		const chartHeight = height - padding.top - padding.bottom;

		// Draw grid
		ctx.strokeStyle = '#374151';
		ctx.lineWidth = 0.5;

		for (let i = 0; i <= 5; i++) {
			const x = padding.left + (i / 5) * chartWidth;
			ctx.beginPath();
			ctx.moveTo(x, padding.top);
			ctx.lineTo(x, padding.top + chartHeight);
			ctx.stroke();

			const y = padding.top + (i / 5) * chartHeight;
			ctx.beginPath();
			ctx.moveTo(padding.left, y);
			ctx.lineTo(padding.left + chartWidth, y);
			ctx.stroke();
		}

		// Draw axes
		ctx.strokeStyle = '#4b5563';
		ctx.lineWidth = 1;
		ctx.beginPath();
		ctx.moveTo(padding.left, padding.top + chartHeight);
		ctx.lineTo(padding.left + chartWidth, padding.top + chartHeight);
		ctx.stroke();

		ctx.beginPath();
		ctx.moveTo(padding.left, padding.top);
		ctx.lineTo(padding.left, padding.top + chartHeight);
		ctx.stroke();

		// Draw line connecting points (efficient frontier)
		if (showLine && points.length > 1) {
			ctx.strokeStyle = '#6b7280';
			ctx.lineWidth = 1.5;
			ctx.beginPath();
			const sortedPoints = [...points].sort((a, b) => a.x - b.x);
			sortedPoints.forEach((point, i) => {
				const x = scaleX(point.x, chartWidth, padding.left);
				const y = scaleY(point.y, chartHeight, padding.top);
				if (i === 0) ctx.moveTo(x, y);
				else ctx.lineTo(x, y);
			});
			ctx.stroke();
		}

		// Draw regular points
		points.forEach((point) => {
			const x = scaleX(point.x, chartWidth, padding.left);
			const y = scaleY(point.y, chartHeight, padding.top);
			drawPoint(ctx, x, y, point);
		});

		// Draw highlighted points (on top)
		highlights.forEach((point) => {
			const x = scaleX(point.x, chartWidth, padding.left);
			const y = scaleY(point.y, chartHeight, padding.top);
			drawPoint(ctx, x, y, { ...point, size: (point.size || 4) * 1.5 });

			// Draw label
			if (point.label) {
				ctx.fillStyle = '#ffffff';
				ctx.font = '10px monospace';
				ctx.textAlign = 'left';
				ctx.fillText(point.label, x + 10, y - 5);
			}
		});

		// Draw axis labels
		ctx.fillStyle = '#9ca3af';
		ctx.font = '10px monospace';

		// X-axis labels
		ctx.textAlign = 'center';
		ctx.textBaseline = 'top';
		for (let i = 0; i <= 5; i++) {
			const value = xMin + (i / 5) * (xMax - xMin);
			const x = padding.left + (i / 5) * chartWidth;
			ctx.fillText(value.toFixed(1), x, padding.top + chartHeight + 8);
		}
		ctx.fillText(xLabel, width / 2, height - 10);

		// Y-axis labels
		ctx.textAlign = 'right';
		ctx.textBaseline = 'middle';
		for (let i = 0; i <= 5; i++) {
			const value = yMax - (i / 5) * (yMax - yMin);
			const y = padding.top + (i / 5) * chartHeight;
			ctx.fillText(value.toFixed(1), padding.left - 8, y);
		}

		// Y-axis label (rotated)
		ctx.save();
		ctx.translate(15, height / 2);
		ctx.rotate(-Math.PI / 2);
		ctx.textAlign = 'center';
		ctx.fillText(yLabel, 0, 0);
		ctx.restore();
	}

	function handleMouseMove(event: MouseEvent) {
		const rect = canvas.getBoundingClientRect();
		const mouseX = event.clientX - rect.left;
		const mouseY = event.clientY - rect.top;

		const padding = { top: 30, right: 30, bottom: 50, left: 60 };
		const chartWidth = width - padding.left - padding.right;
		const chartHeight = height - padding.top - padding.bottom;

		// Find closest point
		let closestPoint: Point | null = null;
		let minDist = 20; // Max hover distance

		for (const point of allPoints) {
			const x = scaleX(point.x, chartWidth, padding.left);
			const y = scaleY(point.y, chartHeight, padding.top);
			const dist = Math.sqrt((mouseX - x) ** 2 + (mouseY - y) ** 2);
			if (dist < minDist) {
				minDist = dist;
				closestPoint = point;
			}
		}

		if (closestPoint !== null) {
			const label = closestPoint.label || '';
			tooltipContent = `${label ? label + ': ' : ''}(${closestPoint.x.toFixed(2)}, ${closestPoint.y.toFixed(2)})`;
			tooltipX = event.clientX + 10;
			tooltipY = event.clientY + 10;
			tooltipVisible = true;
		} else {
			tooltipVisible = false;
		}
	}

	function handleMouseLeave() {
		tooltipVisible = false;
	}

	$: if (canvas && points.length > 0) {
		draw();
	}

	onMount(() => {
		draw();
	});
</script>

<div class="scatter-chart-container relative">
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
	.scatter-chart-container {
		display: inline-block;
	}
</style>
