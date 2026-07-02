<script lang="ts">
	import { onMount, onDestroy } from 'svelte';

	interface Trade {
		mae?: number; // Max adverse excursion %
		mfe?: number; // Max favorable excursion %
		return_pct: number;
		pnl: number;
	}

	export let trades: Trade[] = [];
	export let height: number = 200;

	let canvas: HTMLCanvasElement;
	let ctx: CanvasRenderingContext2D | null = null;

	$: if (ctx && trades.length > 0) {
		drawChart();
	}

	onMount(() => {
		if (!canvas) return;
		ctx = canvas.getContext('2d');
		
		const resizeObserver = new ResizeObserver(() => {
			if (canvas) {
				canvas.width = canvas.parentElement?.clientWidth || 400;
				canvas.height = height;
				drawChart();
			}
		});
		if (canvas.parentElement) {
			resizeObserver.observe(canvas.parentElement);
		}

		// Initial size
		canvas.width = canvas.parentElement?.clientWidth || 400;
		canvas.height = height;
		drawChart();

		return () => resizeObserver.disconnect();
	});

	function drawChart() {
		if (!ctx || !canvas) return;

		const width = canvas.width;
		const chartHeight = canvas.height;
		const padding = { top: 20, right: 20, bottom: 30, left: 50 };
		const plotWidth = width - padding.left - padding.right;
		const plotHeight = chartHeight - padding.top - padding.bottom;

		// Clear canvas
		ctx.fillStyle = '#050505';
		ctx.fillRect(0, 0, width, chartHeight);

		// Filter trades with MAE/MFE data
		const validTrades = trades.filter(t => t.mae !== undefined && t.mfe !== undefined);
		
		if (validTrades.length === 0) {
			ctx.fillStyle = '#6b7280';
			ctx.font = '12px sans-serif';
			ctx.textAlign = 'center';
			ctx.fillText('No MAE/MFE data available', width / 2, chartHeight / 2);
			return;
		}

		// Calculate bounds (MAE is negative, MFE is positive typically)
		const maeValues = validTrades.map(t => Math.abs(t.mae || 0));
		const mfeValues = validTrades.map(t => Math.abs(t.mfe || 0));
		const maxMae = Math.max(...maeValues, 1);
		const maxMfe = Math.max(...mfeValues, 1);

		// Draw grid
		ctx.strokeStyle = '#1a1a1a';
		ctx.lineWidth = 1;
		
		// Vertical grid lines
		for (let i = 0; i <= 4; i++) {
			const x = padding.left + (plotWidth / 4) * i;
			ctx.beginPath();
			ctx.moveTo(x, padding.top);
			ctx.lineTo(x, chartHeight - padding.bottom);
			ctx.stroke();
		}
		
		// Horizontal grid lines
		for (let i = 0; i <= 4; i++) {
			const y = padding.top + (plotHeight / 4) * i;
			ctx.beginPath();
			ctx.moveTo(padding.left, y);
			ctx.lineTo(width - padding.right, y);
			ctx.stroke();
		}

		// Draw axes
		ctx.strokeStyle = '#222';
		ctx.lineWidth = 1;
		
		// Y-axis
		ctx.beginPath();
		ctx.moveTo(padding.left, padding.top);
		ctx.lineTo(padding.left, chartHeight - padding.bottom);
		ctx.stroke();
		
		// X-axis
		ctx.beginPath();
		ctx.moveTo(padding.left, chartHeight - padding.bottom);
		ctx.lineTo(width - padding.right, chartHeight - padding.bottom);
		ctx.stroke();

		// Draw diagonal reference line (edge ratio = 1)
		ctx.strokeStyle = '#333';
		ctx.setLineDash([5, 5]);
		ctx.beginPath();
		ctx.moveTo(padding.left, chartHeight - padding.bottom);
		const diagEnd = Math.min(plotWidth, plotHeight);
		ctx.lineTo(padding.left + diagEnd, chartHeight - padding.bottom - diagEnd);
		ctx.stroke();
		ctx.setLineDash([]);

		// Draw scatter points
		validTrades.forEach(trade => {
			const mae = Math.abs(trade.mae || 0);
			const mfe = Math.abs(trade.mfe || 0);
			
			const x = padding.left + (mae / maxMae) * plotWidth;
			const y = chartHeight - padding.bottom - (mfe / maxMfe) * plotHeight;
			
			// Color based on whether trade was profitable
			const isWinner = trade.pnl > 0;
			ctx!.fillStyle = isWinner ? 'rgba(34, 197, 94, 0.6)' : 'rgba(239, 68, 68, 0.6)';
			
			ctx!.beginPath();
			ctx!.arc(x, y, 4, 0, Math.PI * 2);
			ctx!.fill();
		});

		// Draw axis labels
		ctx.fillStyle = '#9ca3af';
		ctx.font = '10px sans-serif';
		ctx.textAlign = 'center';
		
		// X-axis label
		ctx.fillText('MAE (Max Adverse Excursion %)', width / 2, chartHeight - 5);
		
		// Y-axis label
		ctx.save();
		ctx.translate(12, chartHeight / 2);
		ctx.rotate(-Math.PI / 2);
		ctx.fillText('MFE (Max Favorable Excursion %)', 0, 0);
		ctx.restore();

		// Draw axis values
		ctx.fillStyle = '#6b7280';
		ctx.textAlign = 'right';
		
		// Y-axis values
		for (let i = 0; i <= 4; i++) {
			const val = (maxMfe / 4) * i;
			const y = chartHeight - padding.bottom - (plotHeight / 4) * i;
			ctx.fillText(val.toFixed(1) + '%', padding.left - 5, y + 3);
		}
		
		// X-axis values
		ctx.textAlign = 'center';
		for (let i = 0; i <= 4; i++) {
			const val = (maxMae / 4) * i;
			const x = padding.left + (plotWidth / 4) * i;
			ctx.fillText(val.toFixed(1) + '%', x, chartHeight - padding.bottom + 15);
		}

		// Draw legend
		ctx.font = '9px sans-serif';
		ctx.textAlign = 'left';
		
		// Winners
		ctx.fillStyle = 'rgba(34, 197, 94, 0.8)';
		ctx.beginPath();
		ctx.arc(width - 80, 15, 4, 0, Math.PI * 2);
		ctx.fill();
		ctx.fillStyle = '#9ca3af';
		ctx.fillText('Winners', width - 72, 18);
		
		// Losers
		ctx.fillStyle = 'rgba(239, 68, 68, 0.8)';
		ctx.beginPath();
		ctx.arc(width - 80, 28, 4, 0, Math.PI * 2);
		ctx.fill();
		ctx.fillStyle = '#9ca3af';
		ctx.fillText('Losers', width - 72, 31);
	}
</script>

<div class="trade-scatter-chart">
	<div class="chart-header">
		<span class="text-[10px] text-[#666] uppercase tracking-wider">Trade Quality (MAE vs MFE)</span>
		<span class="text-[9px] text-[#555]">Points above diagonal = good edge ratio</span>
	</div>
	<canvas bind:this={canvas} style="height: {height}px"></canvas>
</div>

<style>
	.trade-scatter-chart {
		width: 100%;
	}
	.chart-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0.25rem 0;
	}
	canvas {
		width: 100%;
	}
</style>
